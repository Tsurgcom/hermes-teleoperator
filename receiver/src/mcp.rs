// Core interface: McpManager::{new(PathBuf), catalog(), call(&str, Value), close()}.
// Reads only <home>/config.json: mcp_servers + explicit expose_mcp server allowlist.
// Request deadline: mcp_request_timeout_ms (default 30_000, range 1..=300_000).
// Unix stdio only; no Hermes installation/auth stores or Python runtime required.
// TDD: initial fixture run failed (catalog length 0 rather than 2) with stubs.
// Python is test-fixture-only. All production dependencies: std, serde_json, libc.

use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet, VecDeque};
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc::{self, Receiver, TrySendError},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const MAX_FRAME: usize = 16 * 1024 * 1024;
const MAX_CONFIG: usize = 1024 * 1024;
const MAX_TOOLS: usize = 4096;
const MAX_PAGES: usize = 128;
const STDERR_LIMIT: usize = 8192;

type ToolMap = BTreeMap<String, (String, String)>;

pub struct McpManager {
    home: PathBuf,
    clients: BTreeMap<String, Client>,
    schemas: Vec<Value>,
    tools: ToolMap,
    loaded: bool,
}

impl McpManager {
    pub fn new(home: PathBuf) -> Self {
        Self {
            home,
            clients: BTreeMap::new(),
            schemas: Vec::new(),
            tools: BTreeMap::new(),
            loaded: false,
        }
    }

    pub fn catalog(&mut self) -> Result<Vec<Value>, String> {
        if !self.loaded {
            if let Err(error) = self.load() {
                self.close();
                return Err(error);
            }
            self.loaded = true;
        }
        Ok(self.schemas.clone())
    }

    fn load(&mut self) -> Result<(), String> {
        let path = self.home.join("config.json");
        let mut bytes = Vec::new();
        match File::open(&path) {
            Ok(file) => {
                file.take((MAX_CONFIG + 1) as u64)
                    .read_to_end(&mut bytes)
                    .map_err(|e| format!("read MCP config: {e}"))?;
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(format!("open MCP config: {e}")),
        }
        if bytes.len() > MAX_CONFIG {
            return Err("MCP config exceeds 1 MiB".into());
        }
        let config: Value =
            serde_json::from_slice(&bytes).map_err(|e| format!("invalid MCP config JSON: {e}"))?;
        if !config.is_object() {
            return Err("MCP config must be an object".into());
        }
        let allow = match config.get("expose_mcp") {
            None => return Ok(()),
            Some(v) => v
                .as_array()
                .ok_or("expose_mcp must be an explicit array of server names")?,
        };
        if allow.is_empty() {
            return Ok(());
        }
        if allow.len() > 128 {
            return Err("too many exposed MCP servers (maximum 128)".into());
        }
        let servers = config
            .get("mcp_servers")
            .and_then(Value::as_object)
            .ok_or("mcp_servers must be an object")?;
        let timeout_ms = match config.get("mcp_request_timeout_ms") {
            None => 30_000,
            Some(v) => v
                .as_u64()
                .filter(|n| (1..=300_000).contains(n))
                .ok_or("mcp_request_timeout_ms must be an integer in 1..=300000")?,
        };
        let mut selected = BTreeMap::new();
        for value in allow {
            let name = value
                .as_str()
                .ok_or("expose_mcp entries must be server names")?;
            if !valid_server_name(name) {
                return Err(format!("invalid MCP server name {name:?}: use 1..64 ASCII letters, digits, hyphens or underscores, without '__' or a trailing underscore"));
            }
            if selected.contains_key(name) {
                return Err(format!("duplicate expose_mcp server {name}"));
            }
            let spec = servers
                .get(name)
                .ok_or_else(|| format!("exposed MCP server {name} is not configured"))?;
            validate_spec(spec).map_err(|e| format!("MCP server {name}: {e}"))?;
            selected.insert(name.to_owned(), spec);
        }
        let mut catalog_bytes = 0usize;
        for (name, spec) in selected {
            let client = Client::spawn(spec, Duration::from_millis(timeout_ms))
                .map_err(|e| format!("MCP server {name}: {e}"))?;
            self.clients.insert(name.clone(), client);
            let client = self.clients.get_mut(&name).unwrap();
            let initialized = client
                .request(
                    "initialize",
                    json!({
                        "protocolVersion":"2024-11-05", "capabilities":{},
                        "clientInfo":{"name":"hermes-teleoperator-receiver","version":"1.0"}
                    }),
                )
                .map_err(|e| format!("MCP server {name}: {e}"))?;
            if initialized.get("protocolVersion").and_then(Value::as_str) != Some("2024-11-05") {
                return Err(format!("MCP server {name}: unsupported initialize protocolVersion (requires 2024-11-05)"));
            }
            client
                .notify("notifications/initialized", json!({}))
                .map_err(|e| format!("MCP server {name}: {e}"))?;
            let mut cursor: Option<String> = None;
            let mut cursors = HashSet::new();
            for page in 0..MAX_PAGES {
                let params = cursor
                    .as_ref()
                    .map(|c| json!({"cursor":c}))
                    .unwrap_or_else(|| json!({}));
                let result = client
                    .request("tools/list", params)
                    .map_err(|e| format!("MCP server {name}: {e}"))?;
                catalog_bytes += serde_json::to_vec(&result)
                    .map_err(|e| format!("encode MCP catalog: {e}"))?
                    .len();
                if catalog_bytes > MAX_FRAME {
                    return Err("MCP catalog exceeds 16 MiB aggregate limit".into());
                }
                let tools = result
                    .get("tools")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        format!("MCP server {name}: tools/list must return a tools array")
                    })?;
                if self.schemas.len() + tools.len() > MAX_TOOLS {
                    return Err(format!("MCP tools/list exceeds {MAX_TOOLS} tools"));
                }
                for tool in tools {
                    let actual = tool
                        .get("name")
                        .and_then(Value::as_str)
                        .ok_or("MCP tool is missing its name")?;
                    if actual.is_empty()
                        || actual.len() > 128
                        || !actual
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
                    {
                        return Err(format!("invalid MCP tool name {actual:?}"));
                    }
                    let exposed = format!("mcp__{name}__{actual}");
                    if self.tools.contains_key(&exposed) {
                        return Err(format!("duplicate MCP tool {exposed}"));
                    }
                    let parameters = tool
                        .get("inputSchema")
                        .filter(|v| v.is_object())
                        .ok_or_else(|| format!("MCP tool {exposed} has no object inputSchema"))?;
                    let description = match tool.get("description") {
                        None => "",
                        Some(v) => v.as_str().ok_or("MCP tool description must be a string")?,
                    };
                    self.schemas.push(
                        json!({"name":exposed,"description":description,"parameters":parameters}),
                    );
                    self.tools
                        .insert(exposed, (name.clone(), actual.to_owned()));
                }
                match result.get("nextCursor") {
                    None | Some(Value::Null) => break,
                    Some(v) => {
                        let next = v
                            .as_str()
                            .filter(|s| !s.is_empty() && s.len() <= 4096)
                            .ok_or("invalid MCP tools/list nextCursor")?;
                        if !cursors.insert(next.to_owned()) {
                            return Err("MCP tools/list repeated pagination cursor".into());
                        }
                        cursor = Some(next.to_owned());
                        if page + 1 == MAX_PAGES {
                            return Err(format!("MCP tools/list exceeds {MAX_PAGES} pages"));
                        }
                    }
                }
            }
        }
        Ok(())
    }

    pub fn call(&mut self, name: &str, args: Value) -> Result<Value, String> {
        self.catalog()?;
        let (server, actual) = self
            .tools
            .get(name)
            .cloned()
            .ok_or_else(|| format!("MCP tool is not exposed: {name}"))?;
        if !args.is_object() {
            return Err("MCP tool arguments must be an object".into());
        }
        let result = self
            .clients
            .get_mut(&server)
            .ok_or("MCP server is closed")?
            .request("tools/call", json!({"name":actual,"arguments":args}));
        match result {
            Ok(value) => Ok(value),
            Err(error) => {
                self.close();
                Err(format!("MCP server {server}: {error}"))
            }
        }
    }

    pub fn close(&mut self) {
        self.clients.clear();
        self.schemas.clear();
        self.tools.clear();
        self.loaded = false;
    }
}

impl Drop for McpManager {
    fn drop(&mut self) {
        self.close();
    }
}

fn valid_server_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && !name.contains("__")
        && !name.ends_with('_')
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}

fn validate_spec(spec: &Value) -> Result<(), String> {
    if !spec.is_object() {
        return Err("server configuration must be an object".into());
    }
    if spec.get("url").is_some()
        || spec
            .get("transport")
            .and_then(Value::as_str)
            .is_some_and(|t| t != "stdio")
        || spec
            .get("type")
            .and_then(Value::as_str)
            .is_some_and(|t| t != "stdio")
    {
        return Err(
            "only stdio MCP servers are supported; HTTP/SSE transports are not supported".into(),
        );
    }
    let command = spec
        .get("command")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && !s.contains('\0'))
        .ok_or("stdio MCP server requires command")?;
    if command.starts_with("http://") || command.starts_with("https://") {
        return Err("HTTP MCP is not supported; configure a stdio command".into());
    }
    if let Some(args) = spec.get("args") {
        let args = args
            .as_array()
            .ok_or("MCP args must be an array of strings")?;
        if args
            .iter()
            .any(|a| a.as_str().map_or(true, |s| s.contains('\0')))
        {
            return Err("MCP args must be NUL-free strings".into());
        }
    }
    if let Some(env) = spec.get("env") {
        let env = env
            .as_object()
            .ok_or("MCP env must be an object of string values")?;
        if env.iter().any(|(k, v)| {
            k.is_empty() || k.contains(['=', '\0']) || v.as_str().map_or(true, |s| s.contains('\0'))
        }) {
            return Err("MCP env requires valid names and NUL-free string values".into());
        }
    }
    Ok(())
}

struct Client {
    child: Arc<Mutex<Child>>,
    terminated: Arc<AtomicBool>,
    pid: i32,
    stdin: Option<ChildStdin>,
    rx: Receiver<Result<Value, String>>,
    stop: Arc<AtomicBool>,
    threads: Vec<JoinHandle<()>>,
    stderr: Arc<Mutex<VecDeque<u8>>>,
    timeout: Duration,
    next_id: u64,
    closed: bool,
}

fn nonblocking(fd: i32) -> Result<(), String> {
    // The descriptors are owned by this subprocess client, not the receiver's IO.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(format!(
            "set MCP pipe nonblocking: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

impl Client {
    fn spawn(spec: &Value, timeout: Duration) -> Result<Self, String> {
        let mut command = Command::new(spec["command"].as_str().unwrap());
        if let Some(args) = spec.get("args").and_then(Value::as_array) {
            command.args(args.iter().map(|a| a.as_str().unwrap()));
        }
        if let Some(env) = spec.get("env").and_then(Value::as_object) {
            for (k, v) in env {
                command.env(k, v.as_str().unwrap());
            }
        }
        // Inherit ordinary OS environment only. Never consult Hermes auth stores.
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        let mut child = command.spawn().map_err(|e| {
            format!("spawn stdio MCP command (its dependencies must be installed): {e}")
        })?;
        let pid = child.id() as i32;
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        let stdin = child.stdin.take();
        let (tx, rx) = mpsc::sync_channel(32);
        let mut client = Self {
            child: Arc::new(Mutex::new(child)),
            terminated: Arc::new(AtomicBool::new(false)),
            pid,
            stdin,
            rx,
            stop: Arc::new(AtomicBool::new(false)),
            threads: Vec::new(),
            stderr: Arc::new(Mutex::new(VecDeque::new())),
            timeout,
            next_id: 1,
            closed: false,
        };
        let mut stdout = stdout.ok_or("MCP stdout unavailable")?;
        let mut stderr = stderr.ok_or("MCP stderr unavailable")?;
        nonblocking(
            client
                .stdin
                .as_ref()
                .ok_or("MCP stdin unavailable")?
                .as_raw_fd(),
        )?;
        nonblocking(stdout.as_raw_fd())?;
        nonblocking(stderr.as_raw_fd())?;
        let stop = client.stop.clone();
        let child = client.child.clone();
        let terminated = client.terminated.clone();
        client.threads.push(thread::spawn(move || {
            let mut pending = Vec::new();
            let mut buffer = [0u8; 8192];
            'read: while !stop.load(Ordering::Relaxed) {
                match stdout.read(&mut buffer) {
                    Ok(0) => {
                        let _ = tx.try_send(Err(if pending.is_empty() {
                            "MCP subprocess stdout EOF".into()
                        } else {
                            "MCP subprocess EOF in an unterminated JSON line".into()
                        }));
                        break;
                    }
                    Ok(n) => {
                        for &byte in &buffer[..n] {
                            if byte == b'\n' {
                                let parsed = serde_json::from_slice(&pending)
                                    .map_err(|e| format!("invalid MCP JSON line: {e}"));
                                let failed = parsed.is_err();
                                pending.clear();
                                match tx.try_send(parsed) {
                                    Ok(()) if !failed => {}
                                    Ok(())
                                    | Err(TrySendError::Full(_))
                                    | Err(TrySendError::Disconnected(_)) => break 'read,
                                }
                            } else {
                                if pending.len() >= MAX_FRAME {
                                    let _ = tx.try_send(Err(
                                        "MCP JSON line exceeds 16 MiB frame limit".into(),
                                    ));
                                    break 'read;
                                }
                                pending.push(byte);
                            }
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2))
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(e) => {
                        let _ = tx.try_send(Err(format!("read MCP stdout: {e}")));
                        break;
                    }
                }
            }
            // EOF/frame failure is fatal even while the manager is idle.
            terminate_process(&child, &terminated, pid);
        }));
        let stop = client.stop.clone();
        let tail = client.stderr.clone();
        client.threads.push(thread::spawn(move || {
            let mut buffer = [0u8; 8192];
            while !stop.load(Ordering::Relaxed) {
                match stderr.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(n) => {
                        if let Ok(mut bytes) = tail.lock() {
                            let excess = (bytes.len() + n).saturating_sub(STDERR_LIMIT);
                            bytes.drain(..excess);
                            bytes.extend(&buffer[..n]);
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2))
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
        }));
        Ok(client)
    }

    fn send(&mut self, value: &Value, deadline: Instant) -> Result<(), String> {
        let mut bytes = serde_json::to_vec(value).map_err(|e| format!("encode MCP frame: {e}"))?;
        if bytes.len() > MAX_FRAME {
            return Err("outgoing MCP JSON line exceeds 16 MiB frame limit".into());
        }
        bytes.push(b'\n');
        let stdin = self.stdin.as_mut().ok_or("MCP subprocess is closed")?;
        let mut offset = 0;
        while offset < bytes.len() {
            if Instant::now() >= deadline {
                return Err("MCP request timed out writing stdin".into());
            }
            match stdin.write(&bytes[offset..]) {
                Ok(0) => return Err("MCP subprocess closed stdin".into()),
                Ok(n) => offset += n,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(2))
                }
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(e) => return Err(format!("write MCP stdin: {e}")),
            }
        }
        Ok(())
    }

    fn notify(&mut self, method: &str, params: Value) -> Result<(), String> {
        let result = self.send(
            &json!({"jsonrpc":"2.0","method":method,"params":params}),
            Instant::now() + self.timeout,
        );
        if result.is_err() {
            self.shutdown();
        }
        result
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let result = self.request_inner(method, params);
        if let Err(error) = result {
            self.shutdown();
            let tail = self
                .stderr
                .lock()
                .map(|b| {
                    String::from_utf8_lossy(&b.iter().copied().collect::<Vec<_>>()).into_owned()
                })
                .unwrap_or_default();
            return Err(if tail.is_empty() {
                error
            } else {
                format!("{error}; MCP stderr tail: {tail}")
            });
        }
        result
    }

    fn request_inner(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let deadline = Instant::now() + self.timeout;
        let id = self.next_id;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or("MCP request id exhausted")?;
        self.send(
            &json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}),
            deadline,
        )?;
        // Bound unsolicited notifications/requests as well as elapsed time.
        for _ in 0..1024 {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .ok_or_else(|| format!("MCP {method} request timed out"))?;
            let frame = self.rx.recv_timeout(remaining).map_err(|e| match e {
                mpsc::RecvTimeoutError::Timeout => format!("MCP {method} request timed out"),
                mpsc::RecvTimeoutError::Disconnected => {
                    "MCP stdout reader disconnected (EOF or frame queue overflow)".into()
                }
            })??;
            if frame.get("jsonrpc").and_then(Value::as_str) != Some("2.0") || !frame.is_object() {
                return Err("invalid MCP JSON-RPC frame".into());
            }
            if frame.get("method").is_some() {
                if !frame["method"].is_string() {
                    return Err("invalid MCP JSON-RPC method".into());
                }
                if let Some(callback_id) = frame.get("id") {
                    // No sampling, elicitation, roots, or other callbacks are exposed.
                    self.send(&json!({"jsonrpc":"2.0","id":callback_id,"error":{"code":-32601,"message":"Client callbacks are disabled"}}), deadline)?;
                }
                continue;
            }
            if frame.get("id") != Some(&json!(id)) {
                return Err("MCP response id does not match outstanding request".into());
            }
            if let Some(error) = frame.get("error") {
                return Err(format!("MCP {method} JSON-RPC error: {error}"));
            }
            return frame
                .get("result")
                .cloned()
                .ok_or_else(|| "MCP response has neither result nor error".into());
        }
        Err("MCP unsolicited message limit exceeded".into())
    }

    fn shutdown(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        self.stop.store(true, Ordering::Relaxed);
        self.stdin.take();
        // The child was spawned as its own process-group leader. Kill descendants
        // even when the leader has already exited, then reap our immediate child.
        terminate_process(&self.child, &self.terminated, self.pid);
        for handle in self.threads.drain(..) {
            let _ = handle.join();
        }
    }
}

fn terminate_process(child: &Mutex<Child>, terminated: &AtomicBool, pid: i32) {
    // Serialize kill+wait so EOF cleanup and explicit close cannot signal a
    // recycled process-group id after the original child has been reaped.
    if let Ok(mut child) = child.lock() {
        if !terminated.swap(true, Ordering::SeqCst) {
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new(body: &str, extra: Value) -> Self {
            let base = std::env::var_os("TMPDIR")
                .map(PathBuf::from)
                .unwrap_or_else(std::env::temp_dir);
            let home = base.join(format!(
                "teleoperator-mcp-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&home).unwrap();
            fs::write(home.join("server.py"), body).unwrap();
            let mut config = json!({"mcp_servers":{"fixture":{"command":"python3","args":["-u", home.join("server.py")],"env":{"FIXTURE_ENV":"explicit"}}},"expose_mcp":["fixture"],"mcp_request_timeout_ms":2000});
            for (key, value) in extra.as_object().unwrap() {
                config[key] = value.clone();
            }
            fs::write(
                home.join("config.json"),
                serde_json::to_vec(&config).unwrap(),
            )
            .unwrap();
            Self(home)
        }
        fn manager(&self) -> McpManager {
            McpManager::new(self.0.clone())
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    /// Deterministic stand-in for a cold interpreter on a busy CI runner. Python start-up is
    /// not the behaviour under test, so it must not be charged to a request deadline: warm the
    /// fixture under a generous budget, then apply the deadline under test.
    const SLOW_START: &str = "import time; time.sleep(1.5)\n";

    /// Switch every already-initialized client to the deadline under test. Spawning and
    /// `initialize` deliberately run under the fixture's generous budget instead, so a slow
    /// interpreter start cannot consume the budget that is meant to test one operation.
    fn apply_operation_deadline(manager: &mut McpManager, budget: Duration) {
        assert!(
            !manager.clients.is_empty(),
            "fixture must be initialized before the deadline under test applies"
        );
        for client in manager.clients.values_mut() {
            client.timeout = budget;
        }
    }

    const SERVER: &str = r#"import json, os, sys
initialized = False
for line in sys.stdin:
    r = json.loads(line)
    m = r.get('method')
    if m == 'notifications/initialized':
        initialized = True
        continue
    if 'id' not in r:
        continue
    if m == 'initialize':
        assert r['params']['protocolVersion'] == '2024-11-05'
        assert not r['params']['capabilities']
        result = {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif m == 'tools/list':
        assert initialized
        if r.get('params', {}).get('cursor') == 'page2':
            result = {'tools':[{'name':'second__tool','inputSchema':{'type':'object'}}]}
        else:
            result = {'tools':[{'name':'echo','description':'Echo fixture','inputSchema':{'type':'object','properties':{'text':{'type':'string'}}}}],'nextCursor':'page2'}
    elif m == 'tools/call':
        result = {'content':[{'type':'text','text':r['params']['name'] + ':' + r['params']['arguments']['text'] + ':' + os.environ['FIXTURE_ENV']}]}
    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)
"#;

    #[test]
    fn catalog_aggregate_size_is_bounded_across_pages() {
        let body = SERVER
            .replace(
                "'description':'Echo fixture'",
                "'description':'x' * (9 * 1024 * 1024)",
            )
            .replace(
                "'name':'second__tool','inputSchema'",
                "'name':'second__tool','description':'x' * (9 * 1024 * 1024),'inputSchema'",
            );
        let fixture = Fixture::new(&body, json!({"mcp_request_timeout_ms":5000}));
        let error = fixture.manager().catalog().unwrap_err();
        assert!(error.contains("catalog exceeds 16 MiB"), "{error}");
    }

    #[test]
    fn allowlist_defaults_to_deny_and_is_not_tool_name_guessing() {
        for extra in [json!({"expose_mcp":[]}), json!({"expose_mcp":null})] {
            let fixture = Fixture::new("raise Exception('must not run')", extra.clone());
            let mut manager = fixture.manager();
            if extra["expose_mcp"].is_null() {
                let mut config: Value =
                    serde_json::from_slice(&fs::read(fixture.0.join("config.json")).unwrap())
                        .unwrap();
                config.as_object_mut().unwrap().remove("expose_mcp");
                fs::write(
                    fixture.0.join("config.json"),
                    serde_json::to_vec(&config).unwrap(),
                )
                .unwrap();
            }
            assert!(manager.catalog().unwrap().is_empty());
            assert!(manager
                .call("mcp__fixture__echo", json!({}))
                .unwrap_err()
                .contains("not exposed"));
        }
    }

    #[test]
    fn rejects_invalid_config_and_http_without_spawning() {
        for (extra, expected) in [
            (json!({"expose_mcp":"fixture"}), "explicit array"),
            (json!({"expose_mcp":["missing"]}), "not configured"),
            (json!({"expose_mcp":["a__b"]}), "invalid MCP server name"),
            (json!({"expose_mcp":["fixture","fixture"]}), "duplicate"),
            (json!({"mcp_request_timeout_ms":0}), "1..=300000"),
            (
                json!({"mcp_servers":{"fixture":{"url":"https://localhost/"}}}),
                "HTTP/SSE",
            ),
            (
                json!({"mcp_servers":{"fixture":{"command":"https://localhost/"}}}),
                "HTTP MCP",
            ),
            (
                json!({"mcp_servers":{"fixture":{"command":"sh","env":{"BAD":4}}}}),
                "env requires",
            ),
            (
                json!({"mcp_servers":{"fixture":{"command":"sh","args":[4]}}}),
                "NUL-free strings",
            ),
        ] {
            let fixture = Fixture::new(SERVER, extra);
            let error = fixture.manager().catalog().unwrap_err();
            assert!(
                error.contains(expected),
                "{error:?} does not contain {expected:?}"
            );
        }
    }

    #[test]
    fn refuses_sampling_elicitation_and_arbitrary_callbacks() {
        let prefix = r#"import json, sys
request = json.loads(sys.stdin.readline())
for method in ['sampling/createMessage', 'elicitation/create', 'roots/list', 'other']:
    print(json.dumps({'jsonrpc':'2.0','id':'callback','method':method,'params':{}}), flush=True)
    response = json.loads(sys.stdin.readline())
    assert response == {'jsonrpc':'2.0','id':'callback','error':{'code':-32601,'message':'Client callbacks are disabled'}}
print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2024-11-05'}}), flush=True)
"#;
        let fixture = Fixture::new(&format!("{prefix}{SERVER}"), json!({}));
        assert_eq!(fixture.manager().catalog().unwrap().len(), 2);
    }

    #[test]
    fn drains_stderr_and_timeout_kills_and_reaps_child() {
        // Answers initialization and the catalog, then hangs on the call that is timed.
        let hanging = SERVER.replace(
            "    elif m == 'tools/call':\n",
            "    elif m == 'tools/call':\n        time.sleep(60)\n",
        );
        let body = format!(
            "{SLOW_START}import os, sys\nopen(os.path.join(os.path.dirname(__file__), 'pid'), 'w').write(str(os.getpid()))\nsys.stderr.write('x' * (2 * 1024 * 1024))\nsys.stderr.flush()\n{hanging}"
        );
        let fixture = Fixture::new(&body, json!({"mcp_request_timeout_ms":10000}));
        let mut manager = fixture.manager();
        // Startup and initialization must succeed before the deadline under test applies.
        assert_eq!(manager.catalog().unwrap().len(), 2);
        apply_operation_deadline(&mut manager, Duration::from_millis(300));
        let started = Instant::now();
        let error = manager
            .call("mcp__fixture__echo", json!({"text":"slow"}))
            .unwrap_err();
        assert!(error.contains("timed out"), "{error}");
        assert!(error.len() <= STDERR_LIMIT + 300);
        assert!(error.contains("MCP stderr tail"));
        assert!(started.elapsed() < Duration::from_secs(3));
        let pid: i32 = fs::read_to_string(fixture.0.join("pid"))
            .unwrap()
            .parse()
            .unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1, "child must be reaped");
        assert!(manager.clients.is_empty());
    }

    #[test]
    fn idle_stdout_eof_reaps_the_child_without_another_request() {
        let body = SERVER.replace("    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)", "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)\n    if m == 'tools/list' and r.get('params', {}).get('cursor') == 'page2':\n        sys.stdout.close(); os.close(1)\n        import time; time.sleep(60)");
        let fixture = Fixture::new(&body, json!({}));
        let mut manager = fixture.manager();
        manager.catalog().unwrap();
        let pid = manager.clients["fixture"].pid;
        for _ in 0..100 {
            if unsafe { libc::kill(pid, 0) } == -1 {
                return;
            }
            thread::sleep(Duration::from_millis(5));
        }
        panic!("idle child {pid} was not reaped after stdout EOF");
    }

    #[test]
    fn close_kills_descendants_even_when_the_leader_exits() {
        let prefix = r#"import subprocess, os
child = subprocess.Popen(['sh', '-c', 'sleep 60'])
open(os.path.join(os.path.dirname(__file__), 'descendant'), 'w').write(str(child.pid))
"#;
        let body = format!("{prefix}{SERVER}").replace(
            "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)",
            "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)\n    if m == 'tools/list' and r.get('params', {}).get('cursor') == 'page2':\n        os._exit(0)"
        );
        let fixture = Fixture::new(&body, json!({}));
        let mut manager = fixture.manager();
        manager.catalog().unwrap();
        thread::sleep(Duration::from_millis(20));
        let pid: i32 = fs::read_to_string(fixture.0.join("descendant"))
            .unwrap()
            .parse()
            .unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, 0);
        let started = Instant::now();
        manager.close();
        assert!(started.elapsed() < Duration::from_secs(2));
        // Container PID 1 may leave an adopted zombie; that is dead, not running.
        let mut dead = false;
        for _ in 0..100 {
            let stat = fs::read_to_string(format!("/proc/{pid}/stat"));
            if stat.is_err()
                || stat
                    .unwrap()
                    .split(") ")
                    .nth(1)
                    .is_some_and(|s| s.starts_with('Z'))
            {
                dead = true;
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(dead, "MCP descendant {pid} survived close");
    }

    #[test]
    fn blocked_subprocess_stdin_obeys_the_request_deadline() {
        // Respond through catalog, then stop reading before the large call.
        let body = SERVER.replace("    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)", "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}), flush=True)\n    if m == 'tools/list' and r.get('params', {}).get('cursor') == 'page2':\n        import time; time.sleep(60)");
        let body = format!("{SLOW_START}{body}");
        let fixture = Fixture::new(&body, json!({"mcp_request_timeout_ms":10000}));
        let mut manager = fixture.manager();
        // Startup and initialization must succeed before the deadline under test applies.
        assert_eq!(manager.catalog().unwrap().len(), 2);
        apply_operation_deadline(&mut manager, Duration::from_millis(200));
        let started = Instant::now();
        let error = manager
            .call(
                "mcp__fixture__echo",
                json!({"text":"x".repeat(2 * 1024 * 1024)}),
            )
            .unwrap_err();
        assert!(error.contains("timed out writing stdin"), "{error}");
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn malformed_eof_and_oversized_frames_fail_cleanly() {
        for (body, expected) in [
            ("print('not json', flush=True)", "invalid MCP JSON line"),
            ("pass", "EOF"),
            (
                "import sys; sys.stdout.write('{'); sys.stdout.flush()",
                "unterminated",
            ),
            (
                "import sys; sys.stdout.write('x' * (16 * 1024 * 1024 + 1)); sys.stdout.flush()",
                "16 MiB",
            ),
            (
                "print('{\"jsonrpc\":\"2.0\",\"id\":99,\"result\":{}}', flush=True)",
                "does not match",
            ),
            (
                "print('{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"code\":-32603}}', flush=True)",
                "JSON-RPC error",
            ),
        ] {
            let fixture = Fixture::new(body, json!({"mcp_request_timeout_ms":5000}));
            let mut manager = fixture.manager();
            let error = manager.catalog().unwrap_err();
            assert!(
                error.contains(expected),
                "{error:?} does not contain {expected:?}"
            );
            assert!(manager.clients.is_empty());
        }
    }

    #[test]
    fn duplicate_tools_repeated_cursor_and_tool_count_are_bounded() {
        for (original, replacement, expected) in [
            ("'second__tool'", "'echo'", "duplicate MCP tool"),
            (
                "result = {'tools':[{'name':'second__tool','inputSchema':{'type':'object'}}]}",
                "result = {'tools':[], 'nextCursor':'page2'}",
                "repeated pagination cursor",
            ),
            (
                "result = {'tools':[{'name':'second__tool','inputSchema':{'type':'object'}}]}",
                "result = {'tools':[{'name':'t'+str(i),'inputSchema':{}} for i in range(4096)]}",
                "exceeds 4096 tools",
            ),
        ] {
            let fixture = Fixture::new(&SERVER.replace(original, replacement), json!({}));
            let error = fixture.manager().catalog().unwrap_err();
            assert!(error.contains(expected), "{error}");
        }
    }

    #[test]
    fn server_name_cannot_end_at_an_ambiguous_separator() {
        let fixture = Fixture::new(
            SERVER,
            json!({"expose_mcp":["fixture_"],"mcp_servers":{"fixture_":{"command":"python3"}}}),
        );
        let error = fixture.manager().catalog().unwrap_err();
        assert!(error.contains("invalid MCP server name"), "{error}");
    }

    #[test]
    fn handshake_pagination_exact_names_and_call() {
        let fixture = Fixture::new(SERVER, json!({}));
        let mut manager = fixture.manager();
        let catalog = manager.catalog().unwrap();
        assert_eq!(catalog.len(), 2);
        assert_eq!(
            catalog[0],
            json!({"name":"mcp__fixture__echo","description":"Echo fixture","parameters":{"type":"object","properties":{"text":{"type":"string"}}}})
        );
        assert_eq!(catalog[1]["name"], "mcp__fixture__second__tool");
        assert_eq!(
            manager
                .call("mcp__fixture__second__tool", json!({"text":"ok"}))
                .unwrap()["content"][0]["text"],
            "second__tool:ok:explicit"
        );
        assert!(manager.call("mcp__fixture__not_listed", json!({})).is_err());
        manager.close();
    }
}
