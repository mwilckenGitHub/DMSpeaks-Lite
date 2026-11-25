"""INI-owned provider profiles, redacted credentials, key-auth SSH transport."""
from __future__ import annotations
import configparser,io,json,os,re,socket,subprocess,threading,time,urllib.parse,urllib.request
from pathlib import Path
from cryptography.fernet import Fernet
from direction_tags import normalize_direction_tags

ENGINES={'fish_omni','fish_resident','qwen_cli','fish','elevenlabs','openai','azure','google','polly'}
class Connections:
 def __init__(self,path,data,*,create_credentials=True):
  self.path=Path(path).expanduser().resolve();self.data=Path(data).expanduser().resolve();self.lock=threading.RLock();self.tunnels={};self.tunnel_profiles={}
  key=self.data/'credentials.key'
  if create_credentials and not key.exists():
   from connection_files import connection_writer,replace_private
   with connection_writer(self.data):
    if not key.exists() and not (self.data/'credentials.enc').exists():
     replace_private(key,Fernet.generate_key())
  self.cipher=Fernet(key.read_bytes()) if key.exists() else None
  self.secret_file=self.data/'credentials.enc'
 def parser(self):
  p=configparser.ConfigParser(interpolation=None);p.read(self.path,encoding='utf-8-sig');return p
 def profiles(self):
  p=self.parser();result=[]
  for section in p.sections():
   if section.startswith('provider:'):
    value=dict(p[section]);value['id']=section.split(':',1)[1];value['capabilities']=[x.strip() for x in value.get('capabilities','').split(',') if x.strip()];value['has_credentials']=bool(self.secret(value['id'],value))
    for k in ['api_key','token','password','secret_key']:value.pop(k,None)
    result.append(value)
  return result
 def profile(self,ident):
  p=self.parser();section='provider:'+str(ident)
  if section not in p:raise ValueError('Choose a configured TTS connection.')
  value=dict(p[section]);value['id']=ident
  if value.get('engine') not in ENGINES:raise ValueError('Unsupported TTS adapter.')
  return value
 def secrets(self):
  if not self.secret_file.exists():return {}
  if self.cipher is None:raise ValueError('The saved connection credential needs its matching key file.')
  return json.loads(self.cipher.decrypt(self.secret_file.read_bytes()))
 def secret(self,ident,profile=None):
  profile=profile or self.profile(ident);env=profile.get('api_key_env')
  if env and os.environ.get(env):return os.environ[env]
  with self.lock:return self.secrets().get(ident,'')
 def _prepare(self,parser,ident,values):
  if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',ident):raise ValueError('Use a short connection ID with letters, digits, underscores or hyphens.')
  if not isinstance(values,dict):raise ValueError('Supply connection settings as an object.')
  section='provider:'+ident;before=dict(parser[section]) if section in parser else None
  incoming={k:normalize_direction_tags(v) if k=='direction_tags' else ','.join(v) if k=='capabilities' and isinstance(v,list) else str(v) for k,v in values.items() if k not in {'api_key','has_credentials','id','password','token','secret_key'}}
  candidate={**(before or {}),**incoming}
  for k in ['api_key','password','token','secret_key']:candidate.pop(k,None)
  if 'direction_tags' in candidate:
   candidate['direction_tags']=normalize_direction_tags(candidate['direction_tags'])
  engine=candidate.get('engine');transport=candidate.setdefault('transport','local')
  if engine=='fish' and candidate.get('voice','').strip().startswith('sk-fish-'):raise ValueError('Paste the Fish API token into API token / key, not Default voice. Leave Default voice blank when using a reference sample.')
  if engine not in ENGINES:raise ValueError('Choose a supported provider engine.')
  if transport not in {'local','cloud','ssh'}:raise ValueError('Choose local, cloud or SSH transport.')
  if (engine in {'fish_resident','qwen_cli'} and transport!='local'
      or engine=='fish_omni' and transport not in {'local','ssh'}
      or engine=='polly' and transport!='cloud'):raise ValueError('Choose a transport supported by this adapter.')
  endpoint=candidate.get('endpoint','')
  if endpoint:
   u=urllib.parse.urlsplit(endpoint)
   if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password:raise ValueError('Use an HTTP(S) endpoint without credentials in the URL.')
   if transport=='cloud' and u.scheme!='https':raise ValueError('Cloud connections require HTTPS.')
  if engine=='fish_omni' and transport=='local':
   from fish_community_tts import _validate_endpoint
   _validate_endpoint(endpoint)
  parser[section]=candidate
  return before,candidate
 def _save_many(self,updates,alignment=None,*,expected_revision=None,remove=()):
  from connection_files import commit_files,connection_revision,connection_writer,replace_private
  with self.lock,connection_writer(self.path.parent,'.'+self.path.name+'.lock'),connection_writer(self.data):
   if expected_revision is not None and expected_revision!=connection_revision(self.path,self.secret_file):
    raise ValueError('Connection settings changed. Reopen the editor before saving.')
   key=self.data/'credentials.key'
   if self.cipher is None and key.exists():
    self.cipher=Fernet(key.read_bytes())
   parser=self.parser();secrets=self.secrets();changed_secret=False;changed_tunnels=[]
   tunnel_fields=('transport','ssh_host','ssh_port','remote_host','remote_port','ssh_key')
   for ident,values,api_key in updates:
    before,after=self._prepare(parser,ident,values)
    if before and any(before.get(k)!=after.get(k) for k in tunnel_fields):changed_tunnels.append(ident)
    if api_key is not None:
     if not isinstance(api_key,str):raise ValueError('A connection credential must be text.')
     if api_key:secrets[ident]=api_key
     else:secrets.pop(ident,None)
     changed_secret=True
   for ident in remove:
    if parser.remove_section('provider:'+ident):
     changed_tunnels.append(ident)
    if ident in secrets:
     del secrets[ident]
     changed_secret=True
   if alignment is not None:
    from alignment_backend import validate_settings
    parser['alignment']=validate_settings({**(dict(parser['alignment']) if 'alignment' in parser else {}),**alignment})
   text=io.StringIO();parser.write(text);files=[(self.path,text.getvalue().encode('utf-8'))]
   if changed_secret:
    if self.cipher is None:
     material=Fernet.generate_key()
     replace_private(key,material)
     self.cipher=Fernet(material)
    files.append((self.secret_file,self.cipher.encrypt(json.dumps(secrets).encode())))
   commit_files(files)
   for ident in changed_tunnels:self._discard_tunnel(ident)
 def save(self,ident,values,api_key=None):
  self._save_many([(ident,values,api_key)])
  return next(x for x in self.profiles() if x['id']==ident)
 def remove(self, ident):
  self._save_many([], remove=(ident,))
 def import_ini(self,text):
  parsed=configparser.ConfigParser(interpolation=None)
  try:parsed.read_string(text)
  except configparser.Error:raise ValueError('Invalid INI file. Check section headers and duplicate keys.') from None
  updates=[]
  for section in parsed.sections():
   if section.startswith('provider:'):
    data=dict(parsed[section]);key=data.pop('api_key',None);updates.append((section.split(':',1)[1],data,key))
  alignment=dict(parsed['alignment']) if 'alignment' in parsed else None
  if not updates and alignment is None:raise ValueError('The INI file needs a [provider:name] or [alignment] section.')
  self._save_many(updates,alignment);return len(updates)
 def export_ini(self):
  p=self.parser()
  for section in p.sections():
   for k in ['api_key','password','token','secret_key']:
    if k in p[section]:del p[section][k]
  f=io.StringIO();p.write(f);return f.getvalue()
 @staticmethod
 def _stop_tunnel(process):
  if process.poll() is not None:return
  try:
   process.terminate()
   process.wait(timeout=2)
  except subprocess.TimeoutExpired:
   process.kill()
   process.wait()
  except ProcessLookupError:
   pass
 def _discard_tunnel(self,ident):
  old=self.tunnels.pop(ident,None);self.tunnel_profiles.pop(ident,None)
  if old:self._stop_tunnel(old[0])
 def endpoint(self,profile):
  with self.lock:return self._endpoint(profile)
 def _endpoint(self,profile):
  if profile.get('transport')!='ssh':return profile.get('endpoint','').rstrip('/')
  ident=profile['id'];old=self.tunnels.get(ident);signature=tuple(str(profile.get(key,default)) for key,default in [('ssh_host',''),('ssh_port',22),('remote_host','127.0.0.1'),('remote_port',8000),('ssh_key','')])
  if old and old[0].poll() is None and self.tunnel_profiles.get(ident)==signature:return 'http://127.0.0.1:'+str(old[1])
  if old:self._discard_tunnel(ident)
  host=profile.get('ssh_host','');remote=profile.get('remote_host','127.0.0.1');sshport=int(profile.get('ssh_port',22));remoteport=int(profile.get('remote_port',8000))
  if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*',host) or not re.fullmatch(r'[A-Za-z0-9_.:-]+',remote):raise ValueError('Enter a valid SSH destination and remote service hostname.')
  if not 1<=sshport<=65535 or not 1<=remoteport<=65535:raise ValueError('SSH/service ports must be between1 and65535.')
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  args=['ssh','-N','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=30','-p',str(sshport),'-L',f'127.0.0.1:{port}:{remote}:{remoteport}']
  if profile.get('ssh_key'):args+=['-i',str(Path(profile['ssh_key']).expanduser())]
  args.append(host);process=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
  for _ in range(100):
   if process.poll() is not None:raise ValueError('SSH connection failed: '+process.stderr.read().decode(errors='replace')[-500:])
   try:
    with socket.create_connection(('127.0.0.1',port),timeout=.1):self.tunnels[ident]=(process,port);self.tunnel_profiles[ident]=signature;return 'http://127.0.0.1:'+str(port)
   except OSError:time.sleep(.1)
  self._stop_tunnel(process)
  raise ValueError('SSH tunnel did not become ready. Check key authentication and host trust in your terminal.')
 def close(self):
  with self.lock:
   for ident in list(self.tunnels):self._discard_tunnel(ident)
