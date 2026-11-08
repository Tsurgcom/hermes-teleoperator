use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
struct Client { child: Child, input: Option<ChildStdin>, output: BufReader<std::process::ChildStdout> }
impl Client {
 fn new() -> Self { let mut c=Command::new(env!("CARGO_BIN_EXE_hermes-teleoperator-receiver")).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap(); Self{input:c.stdin.take(),output:BufReader::new(c.stdout.take().unwrap()),child:c} }
 fn request(&mut self,method:&str,params:Value)->Value { writeln!(self.input.as_mut().unwrap(),"{}",json!({"id":7,"method":method,"params":params})).unwrap(); let mut s=String::new();self.output.read_line(&mut s).unwrap();serde_json::from_str(&s).unwrap() }
 fn tool(&mut self,name:&str,args:Value)->Value { self.request("tool",json!({"name":name,"arguments":args})) }
}
impl Drop for Client {fn drop(&mut self){self.input.take();self.child.wait().unwrap();}}
#[test] fn transfers_refuse_overwrite_symlinks_and_bad_checksums() {
 let dir=std::env::current_dir().unwrap().join("target").join(format!("transfer-{}",std::process::id()));std::fs::create_dir_all(&dir).unwrap();let p=dir.join("data");let _=std::fs::remove_file(&p);
 let mut c=Client::new();let digest="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
 let a=json!({"path":p,"data":"YWJj","sha256":digest});assert_eq!(c.request("file_put",a.clone())["result"]["size"],3);
 assert!(c.request("file_put",a)["error"].is_object());assert_eq!(c.request("file_get",json!({"path":p}))["result"]["sha256"],digest);
 assert!(c.request("file_remove",json!({"path":p,"sha256":"wrong"}))["error"].is_object());
 let link=dir.join("link");let _=std::fs::remove_file(&link);std::os::unix::fs::symlink(&p,&link).unwrap();assert!(c.request("file_get",json!({"path":link}))["error"].is_object());
 assert!(c.request("file_remove",json!({"path":p,"sha256":digest}))["result"].is_object());std::fs::remove_dir_all(dir).unwrap();
}
#[test] fn text_tools_and_catalog_work_without_hermes() {
 let mut c=Client::new();let p=std::env::current_dir().unwrap().join("target/text-tool-test");let r=c.tool("write_file",json!({"path":p,"content":"hello\nworld\n"}));assert!(r["result"].is_object(),"{r}");
 assert_eq!(c.tool("read_file",json!({"path":p,"offset":2,"limit":1}))["result"]["content"],"2|world");
 assert!(c.tool("patch",json!({"path":p,"old_string":"world","new_string":"earth"}))["result"].is_object());
 assert!(c.tool("search_files",json!({"path":p,"pattern":"earth"}))["result"]["matches"].as_array().unwrap().len()>0);
 assert!(c.tool("read_file",json!({"path":p,"binary":true}))["error"].is_object());
 let schemas=c.request("search",json!({"query":"read_file"}));assert!(schemas["result"]["tools"].as_array().unwrap().iter().any(|s|s["function"]["name"]=="read_file"));
 assert_eq!(c.request("describe",json!({"name":"read_file"}))["result"]["function"]["parameters"]["type"],"object");std::fs::remove_file(p).unwrap();
}
#[test] fn terminal_preserves_state_and_manages_background() {
 let mut c=Client::new();let a=c.tool("terminal",json!({"command":"cd /; export TELEOP_TEST=kept"}));assert_eq!(a["result"]["exit_code"],0,"{a}");
 let b=c.tool("terminal",json!({"command":"printf '%s:%s' \"$PWD\" \"$TELEOP_TEST\""}));assert!(b["result"]["output"].as_str().unwrap().contains("/:kept"),"{b}");
 let bg=c.tool("terminal",json!({"command":"read x; printf 'got:%s:%s' \"$x\" \"$TELEOP_TEST\"","background":true}));let id=bg["result"]["session_id"].as_str().unwrap();
 assert!(c.tool("process_manage",json!({"action":"submit","session_id":id,"text":"hello"}))["result"].is_object());
 let done=c.tool("process",json!({"action":"wait","session_id":id,"timeout":5}));assert_eq!(done["result"]["exit_code"],0,"{done}");assert!(done["result"]["output"].as_str().unwrap().contains("got:hello:kept"),"{done}");
 let t=c.tool("terminal",json!({"command":"sleep 10","timeout":1}));assert_eq!(t["result"]["timed_out"],true,"{t}");
 assert!(c.tool("terminal",json!({"command":"true","pty":true}))["error"].is_object());
}
#[test] fn skills_are_filesystem_only_and_confined() {
 let home=std::env::current_dir().unwrap().join("target/skills-test");let _=std::fs::remove_dir_all(&home);std::fs::create_dir_all(&home).unwrap();
 let mut cmd=Command::new(env!("CARGO_BIN_EXE_hermes-teleoperator-receiver"));cmd.env("TELEOPERATOR_HOME",&home).stdin(Stdio::piped()).stdout(Stdio::piped());let mut child=cmd.spawn().unwrap();let mut c=Client{input:child.stdin.take(),output:BufReader::new(child.stdout.take().unwrap()),child};
 let create=c.tool("skill_manage",json!({"operations":[{"action":"create","name":"sample","content":"---\nname: sample\ndescription: test\n---\n# Example"}]}));assert!(create["result"].is_object(),"{create}");
 assert!(c.tool("skills_list",json!({}))["result"]["skills"].as_array().unwrap().iter().any(|x|x["name"]=="sample"));
 assert!(c.tool("skill_view",json!({"name":"sample"}))["result"]["content"].as_str().unwrap().contains("Example"));
 assert!(c.tool("skill_manage",json!({"operations":[{"action":"write_file","name":"sample","file_path":"references/guide.md","file_content":"a guide"}]}))["result"].is_object());
 assert_eq!(c.tool("skill_view",json!({"name":"sample","file_path":"references/guide.md"}))["result"]["content"],"a guide");
 assert!(c.tool("skill_manage",json!({"operations":[{"action":"write_file","name":"sample","file_path":"../escape","file_content":"bad"}]}))["error"].is_object());drop(c);std::fs::remove_dir_all(home).unwrap();
}
#[test] fn limits_and_cleanup_are_real() {
 let mut c=Client::new();let huge=c.tool("terminal",json!({"command":"head -c 1200000 /dev/zero | tr '\\0' x"}));assert_eq!(huge["result"]["truncated"],true);assert!(huge["result"]["output"].as_str().unwrap().len()<=1048576);
 let job=c.tool("terminal",json!({"command":"sleep 60 & wait","background":true}));let pid=job["result"]["pid"].as_i64().unwrap()as i32;drop(c);assert_eq!(unsafe{libc::kill(pid,0)},-1);
 let mut c=Client::new();assert!(c.tool("search_files",json!({"pattern":"x","target":123}))["error"].is_object());
 assert!(c.tool("terminal",json!({"command":"true","notify":true}))["error"].is_object());
}
#[test] fn malformed_and_oversized_frames_are_bounded() {
 let mut c=Client::new();writeln!(c.input.as_mut().unwrap(),"not json").unwrap();let mut line=String::new();c.output.read_line(&mut line).unwrap();assert!(serde_json::from_str::<Value>(&line).unwrap()["error"].is_object());assert_eq!(c.request("ping",json!({}))["result"]["protocol"],2);
 let frame=vec![b'x';16*1024*1024+1];let mut child=Command::new(env!("CARGO_BIN_EXE_hermes-teleoperator-receiver")).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();child.stdin.take().unwrap().write_all(&frame).unwrap();let out=child.wait_with_output().unwrap();let r:Value=serde_json::from_slice(&out.stdout).unwrap();assert_eq!(r["error"]["type"],"FrameTooLarge");
}
#[test] fn skill_batches_validate_before_mutation() {
 let home=std::env::current_dir().unwrap().join("target/skill-rollback");let _=std::fs::remove_dir_all(&home);std::fs::create_dir_all(&home).unwrap();let mut child=Command::new(env!("CARGO_BIN_EXE_hermes-teleoperator-receiver")).env("TELEOPERATOR_HOME",&home).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();let mut c=Client{input:child.stdin.take(),output:BufReader::new(child.stdout.take().unwrap()),child};
 let r=c.tool("skill_manage",json!({"operations":[{"action":"create","name":"rollback","content":"---\nname: rollback\ndescription: test\n---\nTest"},{"action":"write_file","name":"rollback","file_path":"../outside","file_content":"bad"}]}));assert!(r["error"].is_object());assert!(!home.join("skills/rollback").exists());drop(c);std::fs::remove_dir_all(home).unwrap();
}
#[test] fn foreground_workdir_is_rejected_without_mutating_shell_or_running_command() {
 let mut c=Client::new();
 let early=c.tool("terminal",json!({"command":"exit 99","workdir":"/","background":false}));assert!(early["error"].is_object(),"{early}");
 let schema=c.request("describe",json!({"name":"terminal"}));let description=schema["result"]["function"]["parameters"]["properties"]["workdir"]["description"].as_str().unwrap();assert!(description.contains("foreground")&&description.contains("background"),"{schema}");
 let setup=c.tool("terminal",json!({"command":"cd /; export REJECTION_TEST=original"}));assert_eq!(setup["result"]["exit_code"],0,"{setup}");
 let marker=std::env::current_dir().unwrap().join("target").join(format!("rejected-marker-{}",std::process::id()));let _=std::fs::remove_file(&marker);
 for dir in ["/", "/tmp", "", "/nonexistent-teleoperator-workdir"] {
  let command=format!("printf ran > '{}'; cd /tmp; export REJECTION_TEST=changed",marker.display());
  let r=c.tool("terminal",json!({"command":command,"workdir":dir}));
  let message=r["error"]["message"].as_str().expect("foreground workdir must fail");
  assert!(message.contains("workdir") && message.contains("cd") && message.contains("background"),"{r}");
  assert!(!marker.exists());
  let check=c.tool("terminal",json!({"command":"printf '%s:%s' \"$PWD\" \"$REJECTION_TEST\""}));
  assert_eq!(check["result"]["output"],"/:original","{check}");
 }
}
#[test] fn background_reports_its_starting_directory() {
 let mut c=Client::new();
 let init=c.tool("terminal",json!({"command":"cd /; export BG_CWD_TEST=inherited"}));assert_eq!(init["result"]["exit_code"],0,"{init}");
 let target=std::env::current_dir().unwrap().canonicalize().unwrap();
 for dir in [Some(target.to_string_lossy().into_owned()),Some(".".to_string()),None] {
  let mut args=json!({"command":"printf '%s:%s' \"$(pwd -P)\" \"$BG_CWD_TEST\"","background":true});
  if let Some(w)=&dir {args["workdir"]=json!(w);}
  let job=c.tool("terminal",args);let reported=job["result"]["cwd"].as_str().expect("background cwd");
  let id=job["result"]["session_id"].as_str().unwrap();
  let done=c.tool("process_manage",json!({"action":"wait","session_id":id,"timeout":5}));
  assert_eq!(done["result"]["exit_code"],0,"{done}");
  let expected=if dir.is_some(){target.to_str().unwrap()}else{"/"};
  assert_eq!(done["result"]["output"],format!("{expected}:inherited"),"{done}");
  assert_eq!(reported,expected,"{job}");
 }
 let check=c.tool("terminal",json!({"command":"pwd -P"}));assert_eq!(check["result"]["output"],"/\n");
}
#[test] fn background_jobs_inherit_shell_environment() {
 let mut c=Client::new();c.tool("terminal",json!({"command":"export JOB_TOKEN=inherited; cd /tmp"}));
 let job=c.tool("terminal",json!({"command":"printf '%s:%s' \"$JOB_TOKEN\" \"$PWD\"","background":true}));let id=job["result"]["session_id"].as_str().unwrap();
 let out=c.tool("process_manage",json!({"action":"wait","session_id":id,"timeout":5}));assert_eq!(out["result"]["output"],"inherited:/tmp","{out}");
 assert_eq!(c.tool("process_manage",json!({"action":"list"}))["result"]["sessions"].as_array().unwrap().len(),1);
}
#[test] fn mcp_tools_are_absent_by_default() {
 let home=std::env::current_dir().unwrap().join("target/mcp-empty");let _=std::fs::remove_dir_all(&home);std::fs::create_dir_all(&home).unwrap();let mut child=Command::new(env!("CARGO_BIN_EXE_hermes-teleoperator-receiver")).env("TELEOPERATOR_HOME",&home).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();let mut c=Client{input:child.stdin.take(),output:BufReader::new(child.stdout.take().unwrap()),child};
 assert!(c.tool("mcp__missing__tool",json!({}))["error"].is_object());assert!(c.request("search",json!({"query":"mcp__"}))["result"]["tools"].as_array().unwrap().is_empty());drop(c);std::fs::remove_dir_all(home).unwrap();
}
#[test] fn ping_is_standalone_v2() { let r=Client::new().request("ping",json!({})); assert_eq!(r["result"]["protocol"],2);assert_eq!(r["result"]["runtime"],"standalone-rust");assert!(r["result"]["capabilities"].is_object()); }
