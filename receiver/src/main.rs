mod catalog;
mod files;
mod mcp;
mod skills;
mod terminal;
mod text_tools;
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
const MAX_FRAME: usize = 16 * 1024 * 1024;
struct Receiver { terminal:terminal::Terminal, mcp:mcp::McpManager }
impl Receiver {
 fn new()->Self {Self{terminal:terminal::Terminal::new(),mcp:mcp::McpManager::new(skills::home())}}
 fn catalog(&mut self)->Result<Vec<Value>,String>{let mut tools=catalog::catalog();tools.extend(self.mcp.catalog()?.into_iter().map(|schema|if schema.get("function").is_some(){schema}else{json!({"type":"function","function":schema})}));Ok(tools)}
 fn dispatch(&mut self,method:&str,params:&Value)->Result<Value,String>{
  if !params.is_object(){return Err("params must be an object".into());}
  match method {
   "ping"=>{files::args(params,&[])?;let mut host=[0u8;256];unsafe{libc::gethostname(host.as_mut_ptr().cast(),host.len());}let end=host.iter().position(|x|*x==0).unwrap_or(host.len());Ok(json!({"protocol":2,"version":env!("CARGO_PKG_VERSION"),"runtime":"standalone-rust","hostname":String::from_utf8_lossy(&host[..end]),"home":std::env::var("HOME").unwrap_or_default(),"cwd":std::env::current_dir().map_err(|e|e.to_string())?,"pid":std::process::id(),"platform":std::env::consts::OS,"teleoperator_home":skills::home(),"capabilities":{"terminal":true,"files":true,"skills":true,"mcp_stdio":true,"background_processes":true,"pty":false,"document_extraction":false,"native_hermes_guards":false,"authority":"SSH user; native Hermes approval and policy guards are absent"}}))},
   "tool"=>{files::args(params,&["name","arguments"])?;let name=files::text(params,"name")?;let empty=json!({});let arguments=params.get("arguments").unwrap_or(&empty);if !arguments.is_object(){return Err("arguments must be an object".into());}catalog::validate(name,arguments)?;match name{
     "terminal"=>self.terminal.run(arguments),"process"|"process_manage"=>self.terminal.manage(arguments),
     "skills_list"|"skill_view"|"skill_manage"=>skills::tool(name,arguments),
     n if n.starts_with("mcp__")=>self.mcp.call(n,arguments.clone()),
     _=>text_tools::tool(name,arguments)}},
   "search"=>{files::args(params,&["query"])?;let q=match params.get("query"){Some(v)=>v.as_str().ok_or("query must be string")?,None=>""}.to_lowercase();Ok(json!({"tools":self.catalog()?.into_iter().filter(|s|q.split_whitespace().all(|w|s.to_string().to_lowercase().contains(w))).take(30).collect::<Vec<_>>()}))},
   "describe"=>{files::args(params,&["name"])?;let name=files::text(params,"name")?;self.catalog()?.into_iter().find(|s|s["function"]["name"]==name).ok_or_else(||format!("Unknown tool: {name}"))},
   "file_get"|"file_put"|"file_remove"=>files::transfer(method,params),
   _=>Err(format!("Unknown method: {method}"))
  }
 }
}
impl Drop for Receiver{fn drop(&mut self){self.mcp.close();}}
fn main(){
 let mut receiver=Receiver::new();let stdin=io::stdin();let mut input=stdin.lock();let stdout=io::stdout();let mut output=stdout.lock();
 loop {
  let mut line=Vec::new();match input.by_ref().take((MAX_FRAME+1)as u64).read_until(b'\n',&mut line){Ok(0)=>break,Err(_)=>break,_=>{}};
  let response=if line.len()>MAX_FRAME{json!({"id":null,"error":{"type":"FrameTooLarge","message":"Request exceeds 16 MiB frame limit"}})}else{
   let mut id=Value::Null;let result=(||{let req:Value=serde_json::from_slice(&line).map_err(|e|e.to_string())?;files::args(&req,&["id","method","params"])?;id=req.get("id").cloned().ok_or("Missing id")?;let method=files::text(&req,"method")?;receiver.dispatch(method,req.get("params").unwrap_or(&json!({})))})();
   match result{Ok(result)=>json!({"id":id,"result":result}),Err(message)=>json!({"id":id,"error":{"type":"ReceiverError","message":message}})}
  };
  let mut encoded=serde_json::to_vec(&response).unwrap_or_default();if encoded.len()+1>MAX_FRAME{encoded=serde_json::to_vec(&json!({"id":response["id"],"error":{"type":"FrameTooLarge","message":"Response exceeds 16 MiB frame limit; narrow request"}})).unwrap();}encoded.push(b'\n');if output.write_all(&encoded).is_err()||output.flush().is_err(){break;}if line.len()>MAX_FRAME{break;}
 }
}
