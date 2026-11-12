use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{ffi::CString, fs::File,io::{Read,Write},os::unix::{fs::MetadataExt,io::{AsRawFd,FromRawFd}},path::{Component,Path,PathBuf}};
#[cfg(test)] use std::time::{SystemTime,UNIX_EPOCH};
pub const MAX_FILE: usize=8*1024*1024;
pub fn text<'a>(v:&'a Value,key:&str)->Result<&'a str,String>{v.get(key).and_then(Value::as_str).ok_or_else(||format!("{key} must be a string"))}
pub fn args(v:&Value,allowed:&[&str])->Result<(),String>{let o=v.as_object().ok_or("arguments must be an object")?;for k in o.keys(){if !allowed.contains(&k.as_str()){return Err(format!("Unsupported argument: {k}"));}}Ok(())}
pub fn path(s:&str)->Result<PathBuf,String>{let p=if s=="~" {PathBuf::from(std::env::var("HOME").map_err(|e|e.to_string())?)}else if let Some(t)=s.strip_prefix("~/"){PathBuf::from(std::env::var("HOME").map_err(|e|e.to_string())?).join(t)}else{PathBuf::from(s)};if p.is_absolute(){Ok(p)}else{Ok(std::env::current_dir().map_err(|e|e.to_string())?.join(p))}}
fn cstr(s:&std::ffi::OsStr)->Result<CString,String>{use std::os::unix::ffi::OsStrExt;CString::new(s.as_bytes()).map_err(|e|e.to_string())}
fn err()->String{std::io::Error::last_os_error().to_string()}
#[cfg(test)] fn nanos()->u128{SystemTime::now().duration_since(UNIX_EPOCH).map(|d|d.as_nanos()).unwrap_or(0)}
// Resolve every directory through file descriptors, never following symlinks.
fn parent(p:&Path)->Result<(File,CString),String>{let name=cstr(p.file_name().ok_or("Path must name a file")?)?;let mut dir=File::open("/").map_err(|e|e.to_string())?;for part in p.parent().ok_or("Missing parent")?.components(){match part {Component::RootDir=>{},Component::Normal(s)=>{let c=cstr(s)?;let fd=unsafe{libc::openat(dir.as_raw_fd(),c.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC|libc::O_DIRECTORY|libc::O_NOFOLLOW)};if fd<0{return Err(err());}dir=unsafe{File::from_raw_fd(fd)};},_=>return Err("Parent traversal is not allowed".into())}}Ok((dir,name))}
fn open_at(dir:&File,name:&CString)->Result<File,String>{let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_CLOEXEC|libc::O_NOFOLLOW|libc::O_NONBLOCK)};if fd<0{return Err(err());}let f=unsafe{File::from_raw_fd(fd)};if !f.metadata().map_err(|e|e.to_string())?.is_file(){return Err("Requires a regular, non-symlink file".into());}Ok(f)}
fn open_regular(p:&Path)->Result<File,String>{let(d,n)=parent(p)?;open_at(&d,&n)}
pub fn bytes(p:&Path)->Result<Vec<u8>,String>{let f=open_regular(p)?;let mut data=Vec::new();f.take((MAX_FILE+1) as u64).read_to_end(&mut data).map_err(|e|e.to_string())?;if data.len()>MAX_FILE{return Err("File exceeds 8 MiB limit; use scp/sftp for larger files".into());}Ok(data)}
pub fn digest(b:&[u8])->String{format!("{:x}",Sha256::digest(b))}
fn same_file(a:&File,b:&File)->bool{match(a.metadata(),b.metadata()){ (Ok(a),Ok(b))=>a.is_file()&&b.is_file()&&a.ino()==b.ino()&&a.dev()==b.dev(),_=>false}}
// Publish the open inode. A name swapped into the staging directory entry cannot change which bytes appear at the destination.
#[cfg(test)] thread_local!{static FORCE_NO_INODE_LINK:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}
#[cfg(test)] thread_local!{static FORCE_NO_STAGE:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}
fn link_inode(dir:&File,src:&File,dest:&CString)->Result<bool,String>{
 #[cfg(test)] if FORCE_NO_INODE_LINK.with(|v|v.get()){return Ok(false);}
 #[cfg(target_os="linux")]{let empty=CString::new("").unwrap();if unsafe{libc::linkat(src.as_raw_fd(),empty.as_ptr(),dir.as_raw_fd(),dest.as_ptr(),libc::AT_EMPTY_PATH)}==0{return Ok(true);}let error=std::io::Error::last_os_error();match error.raw_os_error(){Some(libc::EEXIST)=>Err(error.to_string()),Some(libc::EINVAL)|Some(libc::ENOTSUP)|Some(libc::EPERM)|Some(libc::EACCES)|Some(libc::ENOSYS)=>Ok(false),_=>Err(error.to_string())}}
 #[cfg(not(target_os="linux"))]{let _=(dir,src,dest);Ok(false)}
}
#[cfg(test)] fn rename_noreplace(from_dir:&File,from:&CString,to_dir:&File,to:&CString)->Result<(),i32>{
 #[cfg(target_os="linux")] let rc=unsafe{libc::renameat2(from_dir.as_raw_fd(),from.as_ptr(),to_dir.as_raw_fd(),to.as_ptr(),libc::RENAME_NOREPLACE)};
 #[cfg(target_os="macos")] let rc=unsafe{renameatx_np(from_dir.as_raw_fd(),from.as_ptr(),to_dir.as_raw_fd(),to.as_ptr(),0x4)};
 #[cfg(not(any(target_os="linux",target_os="macos")))] let rc={let _=(from_dir,from,to_dir,to);-1};
 if rc==0{Ok(())}else{Err(std::io::Error::last_os_error().raw_os_error().unwrap_or(libc::EIO))}
}
#[cfg(all(test,target_os="macos"))] extern "C"{fn renameatx_np(fromfd:libc::c_int,from:*const libc::c_char,tofd:libc::c_int,to:*const libc::c_char,flags:libc::c_uint)->libc::c_int;}
fn publish_by_name(_dir:&File,_src:&File,_stage:&CString,_dest:&CString)->Result<(),String>{
 // A name can be exchanged between validation and linkat; never publish it.
 Err("filesystem cannot safely link the staged inode into place".into())
}
fn publish_inode(dir:&File,src:&File,stage:Option<&CString>,dest:&CString)->Result<(),String>{
 if link_inode(dir,src,dest)?{let published=open_at(dir,dest)?;if !same_file(src,&published){return Err("Published file did not match the staged bytes".into());}return Ok(());}
 match stage{Some(stage)=>publish_by_name(dir,src,stage,dest),None=>Err("filesystem cannot link the staged file into place".into())}
}
fn create_stage(dir:&File)->Result<(File,Option<CString>),String>{
 #[cfg(test)] if FORCE_NO_STAGE.with(|v|v.get()){return Err("filesystem cannot create an anonymous transfer staging file".into());}
 #[cfg(target_os="linux")]{let dot=CString::new(".").unwrap();let fd=unsafe{libc::openat(dir.as_raw_fd(),dot.as_ptr(),libc::O_TMPFILE|libc::O_RDWR|libc::O_CLOEXEC,0o600)};if fd>=0{return Ok((unsafe{File::from_raw_fd(fd)},None));}}
 let _=dir;
 // Never create a named transfer stage: its cleanup cannot be made
 // conditional on inode identity when the parent is concurrently writable.
 Err("filesystem cannot create an anonymous transfer staging file".into())
}
fn unlink_if_same(dir:&File,name:&CString,src:&File){if let Ok(found)=open_at(dir,name){if same_file(src,&found){unsafe{libc::unlinkat(dir.as_raw_fd(),name.as_ptr(),0)};}}}

pub fn transfer(method:&str,v:&Value)->Result<Value,String>{let p=path(text(v,"path")?)?;match method{
 "file_get"=>{args(v,&["path"])?;let b=bytes(&p)?;Ok(json!({"data":STANDARD.encode(&b),"sha256":digest(&b),"size":b.len()}))},
 "file_put"=>file_put(&p,v),
 "file_remove"=>file_remove(&p,v),
 _=>Err("Unknown transfer method".into())}}
fn file_put(p:&Path,v:&Value)->Result<Value,String>{args(v,&["path","data","sha256"])?;let encoded=text(v,"data")?;if encoded.len()>MAX_FILE*4/3+8{return Err("File exceeds transfer limit".into());}let b=STANDARD.decode(encoded).map_err(|e|e.to_string())?;let sum=digest(&b);if b.len()>MAX_FILE||sum!=text(v,"sha256")?{return Err("Size or checksum mismatch".into());}let(d,n)=parent(p)?;let(mut f,stage)=create_stage(&d)?;let res=(||{f.write_all(&b).and_then(|_|f.sync_all()).map_err(|e|e.to_string())?;publish_inode(&d,&f,stage.as_ref(),&n)?;Ok(json!({"path":p,"sha256":sum,"size":b.len()}))})();if let Some(stage)=&stage{unlink_if_same(&d,stage,&f);}res}
fn file_remove(_p:&Path,v:&Value)->Result<Value,String>{
 args(v,&["path","sha256"])?;let _=text(v,"sha256")?;
 // Neither platform has an unlink-by-open-inode operation. Even a private
 // directory is writable by another process with the same credentials, so a
 // check followed by unlink could delete its replacement. Fail closed.
 Err("Conditional file removal is unavailable: atomic inode-checked unlink is not supported".into())
}
pub fn replace(_p:&Path,_content:&str)->Result<(),String>{
 // A same-user process can swap a staged name before rename. Refuse before
 // resolving paths or creating staging files until publication is race-free.
 Err("Remote text writes and patches are unavailable: safe atomic replacement is not supported".into())
}

#[cfg(test)]
mod tests{
 use super::*;
 #[test]
 fn replace_refuses_existing_destination_without_side_effects(){
  let root=std::env::temp_dir().join(format!("teleop-replace-refuse-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("file");std::fs::write(&path,b"original").unwrap();
  let metadata=std::fs::metadata(&path).unwrap();
  let result=replace(&path,"changed");
  assert!(result.is_err(),"replacement must be refused");
  let after=std::fs::metadata(&path).unwrap();
  assert_eq!((metadata.dev(),metadata.ino(),metadata.mode(),metadata.len()),(after.dev(),after.ino(),after.mode(),after.len()));
  assert_eq!(std::fs::read(&path).unwrap(),b"original");
  assert_eq!(std::fs::read_dir(&root).unwrap().count(),1);
  std::fs::remove_dir_all(root).unwrap();
 }
 #[test]
 fn replace_refuses_absent_destination_without_artifacts(){
  let root=std::env::temp_dir().join(format!("teleop-replace-absent-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("new");
  assert!(replace(&path,"new content").is_err());
  assert!(!path.exists());
  assert_eq!(std::fs::read_dir(&root).unwrap().count(),0);
  std::fs::remove_dir_all(root).unwrap();
 }
 #[test]
 fn replace_refuses_hardlinked_destination_without_mutation(){
  let root=std::env::temp_dir().join(format!("teleop-replace-linked-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("file");let link=root.join("link");
  std::fs::write(&path,b"original").unwrap();std::fs::hard_link(&path,&link).unwrap();
  let before=std::fs::metadata(&path).unwrap();
  assert!(replace(&path,"changed").is_err());
  let after=std::fs::metadata(&path).unwrap();let linked=std::fs::metadata(&link).unwrap();
  assert_eq!((before.dev(),before.ino(),before.nlink()),(after.dev(),after.ino(),after.nlink()));
  assert_eq!((after.dev(),after.ino()),(linked.dev(),linked.ino()));
  assert_eq!(std::fs::read(&path).unwrap(),b"original");assert_eq!(std::fs::read(&link).unwrap(),b"original");
  assert_eq!(std::fs::read_dir(&root).unwrap().count(),2);
  std::fs::remove_dir_all(root).unwrap();
 }
 #[test]
 fn replace_without_anonymous_stage_leaves_files_untouched(){
  let root=std::env::temp_dir().join(format!("teleop-no-stage-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("file");let hardlink=root.join("hardlink");let new=root.join("new");
  std::fs::write(&path,b"original").unwrap();std::fs::hard_link(&path,&hardlink).unwrap();
  FORCE_NO_STAGE.with(|v|v.set(true));
  let result=replace(&path,"changed");let created=replace(&new,"created");
  FORCE_NO_STAGE.with(|v|v.set(false));
  let bytes=std::fs::read(&path).unwrap();let linked=std::fs::read(&hardlink).unwrap();
  let exists=new.exists();let count=std::fs::read_dir(&root).unwrap().count();
  std::fs::remove_dir_all(root).unwrap();
  assert_eq!(bytes,b"original","fallback mutated the original file");
  assert_eq!(linked,b"original","fallback mutated a hard link");
  assert!(result.is_err());assert!(created.is_err());assert!(!exists);assert_eq!(count,2);
 }
 #[test]
 fn replace_without_inode_link_preserves_existing_hard_links(){
  let root=std::env::temp_dir().join(format!("teleop-replace-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("file");let hardlink=root.join("hardlink");
  std::fs::write(&path,b"old").unwrap();std::fs::hard_link(&path,&hardlink).unwrap();
  FORCE_NO_INODE_LINK.with(|v|v.set(true));
  let result=replace(&path,"new");
  FORCE_NO_INODE_LINK.with(|v|v.set(false));
  let bytes=std::fs::read(&path).unwrap();let linked=std::fs::read(&hardlink).unwrap();
  let count=std::fs::read_dir(&root).unwrap().count();
  std::fs::remove_dir_all(root).unwrap();
  assert_eq!(bytes,b"old","fallback mutated the original file");
  assert_eq!(linked,b"old","fallback mutated a hard link");
  assert!(result.is_err(),"unsafe replacement was accepted");
  assert_eq!(count,2,"unexpected staging artifact");
 }
 #[test]
 fn replace_without_inode_link_refuses_symlink_and_creation(){
  let root=std::env::temp_dir().join(format!("teleop-replace-fallback-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let target=root.join("target");let link=root.join("link");let new=root.join("new");
  std::fs::write(&target,b"original").unwrap();
  std::os::unix::fs::symlink(&target,&link).unwrap();
  FORCE_NO_INODE_LINK.with(|v|v.set(true));
  let refused=replace(&link,"overwrite");let created=replace(&new,"created");
  FORCE_NO_INODE_LINK.with(|v|v.set(false));
  assert!(refused.is_err());assert_eq!(std::fs::read(&target).unwrap(),b"original");
  assert!(created.is_err(),"unsafe creation was accepted");assert!(!new.exists());
  assert_eq!(std::fs::read_dir(&root).unwrap().count(),2);
  std::fs::remove_dir_all(root).unwrap();
 }
 #[test]
 fn removal_fails_closed_without_atomic_conditional_unlink(){
  let root=std::env::temp_dir().join(format!("teleop-remove-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("source");std::fs::write(&path,b"original").unwrap();
  let result=transfer("file_remove",&json!({"path":&path,"sha256":digest(b"original")}));
  assert!(result.is_err(),"unsafe deletion was accepted");
  assert_eq!(std::fs::read(&path).unwrap(),b"original");
  std::fs::remove_dir_all(root).unwrap();
 }
 #[test]
 fn replaced_source_is_never_unlinked(){
  let root=std::env::temp_dir().join(format!("teleop-swap-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  let path=root.join("source");let original=root.join("original");
  std::fs::write(&path,b"original").unwrap();
  let expected=digest(b"original");
  std::fs::rename(&path,&original).unwrap();std::fs::write(&path,b"replacement").unwrap();
  assert!(transfer("file_remove",&json!({"path":&path,"sha256":expected})).is_err());
  assert_eq!(std::fs::read(&path).unwrap(),b"replacement");
  assert_eq!(std::fs::read(&original).unwrap(),b"original");
  std::fs::remove_dir_all(root).unwrap();
 }
 #[test]
 fn name_based_publish_fails_closed_without_touching_destination(){
  let root=std::env::temp_dir().join(format!("teleop-publish-{}-{}",std::process::id(),nanos()));
  std::fs::create_dir_all(&root).unwrap();
  std::fs::write(root.join("stage"),b"staged").unwrap();
  let dir=File::open(&root).unwrap();let stage=CString::new("stage").unwrap();let dest=CString::new("dest").unwrap();
  let src=open_at(&dir,&stage).unwrap();
  let result=publish_by_name(&dir,&src,&stage,&dest);
  assert!(result.is_err(),"name-based publication has a check/link race");
  assert!(!root.join("dest").exists());
  assert_eq!(std::fs::read(root.join("stage")).unwrap(),b"staged");
  std::fs::remove_dir_all(root).unwrap();
 }
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
}
