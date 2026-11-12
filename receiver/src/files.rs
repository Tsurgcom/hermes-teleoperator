use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{ffi::CString, fs::File,io::{Read,Write},os::unix::{fs::MetadataExt,io::{AsRawFd,FromRawFd}},path::{Component,Path,PathBuf},time::{SystemTime,UNIX_EPOCH}};
pub const MAX_FILE: usize=8*1024*1024;
pub fn text<'a>(v:&'a Value,key:&str)->Result<&'a str,String>{v.get(key).and_then(Value::as_str).ok_or_else(||format!("{key} must be a string"))}
pub fn args(v:&Value,allowed:&[&str])->Result<(),String>{let o=v.as_object().ok_or("arguments must be an object")?;for k in o.keys(){if !allowed.contains(&k.as_str()){return Err(format!("Unsupported argument: {k}"));}}Ok(())}
pub fn path(s:&str)->Result<PathBuf,String>{let p=if s=="~" {PathBuf::from(std::env::var("HOME").map_err(|e|e.to_string())?)}else if let Some(t)=s.strip_prefix("~/"){PathBuf::from(std::env::var("HOME").map_err(|e|e.to_string())?).join(t)}else{PathBuf::from(s)};if p.is_absolute(){Ok(p)}else{Ok(std::env::current_dir().map_err(|e|e.to_string())?.join(p))}}
fn cstr(s:&std::ffi::OsStr)->Result<CString,String>{use std::os::unix::ffi::OsStrExt;CString::new(s.as_bytes()).map_err(|e|e.to_string())}
fn err()->String{std::io::Error::last_os_error().to_string()}
fn nanos()->u128{SystemTime::now().duration_since(UNIX_EPOCH).map(|d|d.as_nanos()).unwrap_or(0)}
fn token(prefix:&str)->CString{random_name(prefix)}
fn random_name(prefix:&str)->CString{
 let mut bytes=[0u8;8];
 #[cfg(target_os="linux")] let drew=unsafe{libc::getrandom(bytes.as_mut_ptr() as *mut libc::c_void,bytes.len(),0)}==bytes.len() as isize;
 #[cfg(not(target_os="linux"))] let drew=false;
 let bytes=if drew{bytes}else{let n=nanos().to_ne_bytes();let mut out=[0u8;8];out.copy_from_slice(&n[..8]);out};
 let hex:String=bytes.iter().map(|b|format!("{b:02x}")).collect();
 CString::new(format!(".teleoperator-{prefix}-{}-{hex}",std::process::id())).unwrap()
}
// Resolve every directory through file descriptors, never following symlinks.
fn parent(p:&Path)->Result<(File,CString),String>{let name=cstr(p.file_name().ok_or("Path must name a file")?)?;let mut dir=File::open("/").map_err(|e|e.to_string())?;for part in p.parent().ok_or("Missing parent")?.components(){match part {Component::RootDir=>{},Component::Normal(s)=>{let c=cstr(s)?;let fd=unsafe{libc::openat(dir.as_raw_fd(),c.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC|libc::O_DIRECTORY|libc::O_NOFOLLOW)};if fd<0{return Err(err());}dir=unsafe{File::from_raw_fd(fd)};},_=>return Err("Parent traversal is not allowed".into())}}Ok((dir,name))}
fn open_at(dir:&File,name:&CString)->Result<File,String>{let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC|libc::O_NOFOLLOW|libc::O_NONBLOCK)};if fd<0{return Err(err());}let f=unsafe{File::from_raw_fd(fd)};if !f.metadata().map_err(|e|e.to_string())?.is_file(){return Err("Requires a regular, non-symlink file".into());}Ok(f)}
fn open_regular(p:&Path)->Result<File,String>{let(d,n)=parent(p)?;open_at(&d,&n)}
pub fn bytes(p:&Path)->Result<Vec<u8>,String>{let f=open_regular(p)?;let mut data=Vec::new();f.take((MAX_FILE+1) as u64).read_to_end(&mut data).map_err(|e|e.to_string())?;if data.len()>MAX_FILE{return Err("File exceeds 8 MiB limit; use scp/sftp for larger files".into());}Ok(data)}
pub fn digest(b:&[u8])->String{format!("{:x}",Sha256::digest(b))}
fn same_file(a:&File,b:&File)->bool{match(a.metadata(),b.metadata()){ (Ok(a),Ok(b))=>a.is_file()&&b.is_file()&&a.ino()==b.ino()&&a.dev()==b.dev(),_=>false}}
// Publish the open inode. A name swapped into the staging directory entry cannot change which bytes appear at the destination.
fn link_inode(dir:&File,src:&File,dest:&CString)->Result<bool,String>{
 #[cfg(target_os="linux")]{let empty=CString::new("").unwrap();if unsafe{libc::linkat(src.as_raw_fd(),empty.as_ptr(),dir.as_raw_fd(),dest.as_ptr(),libc::AT_EMPTY_PATH)}==0{return Ok(true);}let error=std::io::Error::last_os_error();match error.raw_os_error(){Some(libc::EEXIST)=>Err(error.to_string()),Some(libc::EINVAL)|Some(libc::ENOTSUP)|Some(libc::EPERM)|Some(libc::EACCES)|Some(libc::ENOSYS)=>Ok(false),_=>Err(error.to_string())}}
 #[cfg(not(target_os="linux"))]{let _=(dir,src,dest);Ok(false)}
}
fn rename_noreplace(from_dir:&File,from:&CString,to_dir:&File,to:&CString)->Result<(),i32>{
 #[cfg(target_os="linux")] let rc=unsafe{libc::renameat2(from_dir.as_raw_fd(),from.as_ptr(),to_dir.as_raw_fd(),to.as_ptr(),libc::RENAME_NOREPLACE)};
 #[cfg(target_os="macos")] let rc=unsafe{renameatx_np(from_dir.as_raw_fd(),from.as_ptr(),to_dir.as_raw_fd(),to.as_ptr(),0x4)};
 #[cfg(not(any(target_os="linux",target_os="macos")))] let rc={let _=(from_dir,from,to_dir,to);-1};
 if rc==0{Ok(())}else{Err(std::io::Error::last_os_error().raw_os_error().unwrap_or(libc::EIO))}
}
fn rename_err(code:i32)->String{std::io::Error::from_raw_os_error(code).to_string()}
#[cfg(target_os="macos")] extern "C"{fn renameatx_np(fromfd:libc::c_int,from:*const libc::c_char,tofd:libc::c_int,to:*const libc::c_char,flags:libc::c_uint)->libc::c_int;}
fn displace(dir:&File,name:&CString,src:&File){let Ok(found)=open_at(dir,name)else{return};if same_file(src,&found){return;}for _ in 0..8{let kept=random_name("kept");if rename_noreplace(dir,name,dir,&kept).is_ok(){return;}}}
fn publish_by_name(dir:&File,src:&File,stage:&CString,dest:&CString)->Result<(),String>{
 let found=open_at(dir,stage)?;if !same_file(src,&found){return Err("Staging file did not match the staged bytes".into());}
 if unsafe{libc::linkat(dir.as_raw_fd(),stage.as_ptr(),dir.as_raw_fd(),dest.as_ptr(),0)}<0{return Err(err());}
 let published=open_at(dir,dest)?;if same_file(src,&published){return Ok(());}
 displace(dir,dest,src);Err("Published file did not match the staged bytes".into())
}
fn publish_inode(dir:&File,src:&File,stage:Option<&CString>,dest:&CString)->Result<(),String>{
 if link_inode(dir,src,dest)?{let published=open_at(dir,dest)?;if !same_file(src,&published){return Err("Published file did not match the staged bytes".into());}return Ok(());}
 match stage{Some(stage)=>publish_by_name(dir,src,stage,dest),None=>Err("filesystem cannot link the staged file into place".into())}
}
fn seal_occupied(dir:&File)->bool{
 let fd=unsafe{libc::dup(dir.as_raw_fd())};if fd<0{return true;}
 let dirp=unsafe{libc::fdopendir(fd)};if dirp.is_null(){unsafe{libc::close(fd)};return true;}
 let mut occupied=false;
 unsafe{loop{let ent=libc::readdir(dirp);if ent.is_null(){break;}let name=std::ffi::CStr::from_ptr((*ent).d_name.as_ptr());let bytes=name.to_bytes();if bytes!=b"."&&bytes!=b".."{occupied=true;break;}}libc::closedir(dirp);}
 occupied
}
fn make_seal(dir:&File)->Result<(File,CString),String>{for _ in 0..8{let name=random_name("seal");if unsafe{libc::mkdirat(dir.as_raw_fd(),name.as_ptr(),0o700)}!=0{continue;}let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC|libc::O_DIRECTORY|libc::O_NOFOLLOW)};if fd<0{continue;}let file=unsafe{File::from_raw_fd(fd)};if seal_occupied(&file){continue;}return Ok((file,name));}Err("could not create a directory for deletion".into())}
fn link_held(dir:&File,held:&File)->Option<CString>{
 #[cfg(target_os="linux")]{let empty=CString::new("").unwrap();for _ in 0..8{let name=random_name("hold");if unsafe{libc::linkat(held.as_raw_fd(),empty.as_ptr(),dir.as_raw_fd(),name.as_ptr(),libc::AT_EMPTY_PATH)}==0{return Some(name);}if std::io::Error::last_os_error().raw_os_error()!=Some(libc::EEXIST){return None;}}}
 #[cfg(not(target_os="linux"))]{let _=(dir,held);}
 None
}
fn name_is_held(dir:&File,name:&CString,held:&File)->bool{open_at(dir,name).map(|found|same_file(held,&found)).unwrap_or(false)}
fn move_held(src_dir:&File,src:&CString,dst_dir:&File,dst:&CString)->bool{
 if let Err(code)=rename_noreplace(src_dir,src,dst_dir,dst){if code!=libc::EEXIST{return false;}}else{return true;}
 for _ in 0..8{let kept=random_name("kept");match rename_noreplace(src_dir,src,dst_dir,&kept){Ok(())=>return true,Err(code) if code==libc::EEXIST=>continue,_=>return false,}}
 false
}
// Put held's inode back. A directory entry that now names a different file is left alone.
fn preserve(seal:&File,dir:&File,leaf:&CString,held:&File,names:&[CString]){
 if name_is_held(dir,leaf,held){for name in names{if name_is_held(seal,name,held){let _=unlink_matching(seal,name,held);}}return;}
 let mut restored=false;
 for name in names{if !name_is_held(seal,name,held){continue;}if !restored{restored=move_held(seal,name,dir,leaf);}else{let _=unlink_matching(seal,name,held);}}
}
fn recheck(dir:&File,name:&CString,held:&File,sum:&str)->Result<(),String>{
 let mut g=open_at(dir,name)?;let current=g.metadata().map_err(|e|e.to_string())?;let h=held.metadata().map_err(|e|e.to_string())?;
 if current.ino()!=h.ino()||current.dev()!=h.dev()||!current.is_file(){return Err("Source changed; refusing deletion".into());}
 let again=read_capped(&mut g)?;if again.len()>MAX_FILE||digest(&again)!=sum{return Err("Source changed; refusing deletion".into());}
 if !name_is_held(dir,name,held){return Err("Source changed; refusing deletion".into());}Ok(())
}
fn unlink_matching(dir:&File,name:&CString,held:&File)->bool{let Ok(found)=open_at(dir,name)else{return false};let Ok(held_meta)=held.metadata()else{return false};let Ok(found_meta)=found.metadata()else{return false};if !found_meta.is_file()||held_meta.ino()!=found_meta.ino()||held_meta.dev()!=found_meta.dev(){return false;}let before=held_meta.nlink();if before==0{return false;}if unsafe{libc::unlinkat(dir.as_raw_fd(),name.as_ptr(),0)}!=0{return false;}held.metadata().map(|meta|meta.nlink()+1==before).unwrap_or(false)}
fn release_seal(dir:&File,seal:&File,name:&CString){unsafe{libc::unlinkat(dir.as_raw_fd(),name.as_ptr(),libc::AT_REMOVEDIR)};let _=seal;}
fn create_stage(dir:&File)->Result<(File,Option<CString>),String>{
 #[cfg(target_os="linux")]{let dot=CString::new(".").unwrap();let fd=unsafe{libc::openat(dir.as_raw_fd(),dot.as_ptr(),libc::O_TMPFILE|libc::O_RDWR|libc::O_CLOEXEC,0o600)};if fd>=0{return Ok((unsafe{File::from_raw_fd(fd)},None));}}
 let stage=random_name("put");let fd=unsafe{libc::openat(dir.as_raw_fd(),stage.as_ptr(),libc::O_RDWR|libc::O_CREAT|libc::O_EXCL|libc::O_CLOEXEC|libc::O_NOFOLLOW,0o600)};if fd<0{return Err(err());}Ok((unsafe{File::from_raw_fd(fd)},Some(stage)))
}
fn unlink_if_same(dir:&File,name:&CString,src:&File){if let Ok(found)=open_at(dir,name){if same_file(src,&found){unsafe{libc::unlinkat(dir.as_raw_fd(),name.as_ptr(),0)};}}}
fn read_capped(file:&mut File)->Result<Vec<u8>,String>{let mut data=Vec::new();Read::by_ref(file).take((MAX_FILE+1) as u64).read_to_end(&mut data).map_err(|e|e.to_string())?;Ok(data)}
fn preserve_mode(dir:&File,name:&CString,src:&File)->Result<(),String>{let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC|libc::O_NOFOLLOW|libc::O_NONBLOCK)};if fd<0{let error=std::io::Error::last_os_error();if error.kind()==std::io::ErrorKind::NotFound{return Ok(());}if error.raw_os_error()==Some(libc::ELOOP){return Err("Requires a regular, non-symlink file".into());}return Err(error.to_string());}let existing=unsafe{File::from_raw_fd(fd)};let meta=existing.metadata().map_err(|e|e.to_string())?;if !meta.is_file(){return Err("Requires a regular, non-symlink file".into());}if unsafe{libc::fchmod(src.as_raw_fd(),(meta.mode()&0o7777) as libc::mode_t)}<0{return Err(err());}Ok(())}
pub fn transfer(method:&str,v:&Value)->Result<Value,String>{let p=path(text(v,"path")?)?;match method{
 "file_get"=>{args(v,&["path"])?;let b=bytes(&p)?;Ok(json!({"data":STANDARD.encode(&b),"sha256":digest(&b),"size":b.len()}))},
 "file_put"=>file_put(&p,v),
 "file_remove"=>file_remove(&p,v),
 _=>Err("Unknown transfer method".into())}}
fn file_put(p:&Path,v:&Value)->Result<Value,String>{args(v,&["path","data","sha256"])?;let encoded=text(v,"data")?;if encoded.len()>MAX_FILE*4/3+8{return Err("File exceeds transfer limit".into());}let b=STANDARD.decode(encoded).map_err(|e|e.to_string())?;let sum=digest(&b);if b.len()>MAX_FILE||sum!=text(v,"sha256")?{return Err("Size or checksum mismatch".into());}let(d,n)=parent(p)?;let(mut f,stage)=create_stage(&d)?;let res=(||{f.write_all(&b).and_then(|_|f.sync_all()).map_err(|e|e.to_string())?;publish_inode(&d,&f,stage.as_ref(),&n)?;Ok(json!({"path":p,"sha256":sum,"size":b.len()}))})();if let Some(stage)=&stage{unlink_if_same(&d,stage,&f);}res}
fn file_remove(p:&Path,v:&Value)->Result<Value,String>{
 args(v,&["path","sha256"])?;let sum=text(v,"sha256")?;let(d,n)=parent(p)?;let mut f=open_at(&d,&n)?;let b=read_capped(&mut f)?;if b.len()>MAX_FILE||digest(&b)!=sum{return Err("Source changed; refusing deletion".into());}
 let(seal,seal_name)=make_seal(&d)?;let data=CString::new("data").unwrap();let hold=link_held(&seal,&f);
 let removed=(||{rename_noreplace(&d,&n,&seal,&data).map_err(rename_err)?;recheck(&seal,&data,&f,sum)?;if !unlink_matching(&seal,&data,&f){return Err("Source changed; refusing deletion".into());}if let Some(hold)=&hold{if !unlink_matching(&seal,hold,&f){return Err("Source changed; refusing deletion".into());}}Ok(())})();
 if let Err(e)=removed{let mut names=Vec::new();if let Some(hold)=hold{names.push(hold);}names.push(data);preserve(&seal,&d,&n,&f,&names);release_seal(&d,&seal,&seal_name);return Err(e);}
 release_seal(&d,&seal,&seal_name);Ok(json!({"removed":p}))
}
pub fn replace(p:&Path,content:&str)->Result<(),String>{if content.len()>MAX_FILE{return Err("Content exceeds 8 MiB".into());}let(d,n)=parent(p)?;let stage=token("w");let fd=unsafe{libc::openat(d.as_raw_fd(),stage.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_CLOEXEC|libc::O_NOFOLLOW,0o600)};if fd<0{return Err(err());}let mut f=unsafe{File::from_raw_fd(fd)};let res=(||{preserve_mode(&d,&n,&f)?;f.write_all(content.as_bytes()).and_then(|_|f.sync_all()).map_err(|e|e.to_string())?;let published=token("pub");publish_inode(&d,&f,Some(&stage),&published)?;if unsafe{libc::renameat(d.as_raw_fd(),published.as_ptr(),d.as_raw_fd(),n.as_ptr())}<0{let error=err();unlink_if_same(&d,&published,&f);return Err(error);}Ok(())})();unlink_if_same(&d,&stage,&f);res}
pub fn write_text(p:&Path,content:&str)->Result<(),String>{if content.len()>MAX_FILE{return Err("Content exceeds 8 MiB".into());}let(d,n)=parent(p)?;let fd=unsafe{libc::openat(d.as_raw_fd(),n.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_CLOEXEC|libc::O_NOFOLLOW|libc::O_NONBLOCK,0o600)};if fd<0{return Err(err());}let mut f=unsafe{File::from_raw_fd(fd)};if !f.metadata().map_err(|e|e.to_string())?.is_file(){return Err("Requires a regular file".into());}f.set_len(0).and_then(|_|f.write_all(content.as_bytes())).map_err(|e|e.to_string())}
#[cfg(test)]
mod tests{
 use super::*;
 use std::sync::{Mutex,atomic::{AtomicBool,Ordering}};
 #[test]
 fn rename_noreplace_does_not_replace_an_existing_file(){
  let root=std::env::temp_dir().join(format!("teleop-rename-{}",std::process::id()));
  let _=std::fs::remove_dir_all(&root);std::fs::create_dir_all(&root).unwrap();
  std::fs::write(root.join("src"),b"source").unwrap();std::fs::write(root.join("dst"),b"dest").unwrap();
  let dir=File::open(&root).unwrap();
  assert_eq!(rename_noreplace(&dir,&CString::new("src").unwrap(),&dir,&CString::new("dst").unwrap()),Err(libc::EEXIST));
  assert_eq!(std::fs::read(root.join("dst")).unwrap(),b"dest");
  assert_eq!(std::fs::read(root.join("src")).unwrap(),b"source");
  let _=std::fs::remove_dir_all(&root);
 }
 #[test]
 fn swapped_aside_name_is_kept_and_original_survives(){
  let payload=vec![b'q';1024*1024];
  let sum=digest(&payload);
  for _ in 0..8{
   let root=std::env::temp_dir().join(format!("teleop-aside-{}-{}",std::process::id(),nanos()));
   let _=std::fs::remove_dir_all(&root);std::fs::create_dir_all(&root).unwrap();
   let path=root.join("move.bin");
   std::fs::write(&path,&payload).unwrap();
   let stop=AtomicBool::new(false);
   let hits=Mutex::new(Vec::<std::path::PathBuf>::new());
   let result=std::thread::scope(|scope|{
    scope.spawn(||{while !stop.load(Ordering::Relaxed){let Ok(entries)=std::fs::read_dir(&root) else{continue};for entry in entries.flatten(){if !entry.file_name().to_string_lossy().starts_with(".teleoperator-seal-"){continue;}let data=entry.path().join("data");let Ok(previous)=std::fs::read(&data) else{continue};if previous.first()!=Some(&b'q'){continue;}let repl=entry.path().join("evil-repl");if std::fs::write(&repl,b"EVIL-BYTES").is_err(){continue;}if std::fs::rename(&repl,&data).is_ok(){hits.lock().unwrap().push(data);}}}});
    let result=transfer("file_remove",&json!({"path":&path,"sha256":&sum}));
    stop.store(true,Ordering::Relaxed);result});
   let hits=hits.into_inner().unwrap();
   if hits.is_empty(){let _=std::fs::remove_dir_all(&root);continue;}
   for hit in &hits{assert_eq!(std::fs::read(hit).ok().as_deref(),Some(b"EVIL-BYTES".as_slice()),"replacement deleted at {}",hit.display());}
   if result.is_err(){
    let mut original=false;let mut stack=vec![root.clone()];
    while let Some(dir)=stack.pop(){let Ok(entries)=std::fs::read_dir(&dir) else{continue};for entry in entries.flatten(){let path=entry.path();if path.is_dir(){stack.push(path);continue;}if std::fs::read(&path).ok().as_deref()==Some(payload.as_slice()){original=true;}}}
    assert!(original,"original bytes were lost after refusing deletion: {result:?}");
   }else{
    assert!(!path.exists(),"reported success while the source file remained: {result:?}");
   }
   let _=std::fs::remove_dir_all(&root);
   return;
  }
  panic!("racer never replaced the aside file");
 }
}
