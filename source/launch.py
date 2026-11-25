"""Cross-platform source-package launcher; no model is needed to start the editor."""
import argparse,importlib.util,json,platform,shutil,sys,threading,webbrowser
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def dependency_report():
 components={'python_3_11_or_newer':sys.version_info>=(3,11)}
 for module in ['cryptography','jsonschema','soundfile','numpy','msgpack']:components[module]=importlib.util.find_spec(module) is not None
 for executable in ['ffmpeg','ffprobe']:components[executable]=shutil.which(executable) is not None
 return {'application':'DMSpeaks','python':platform.python_version(),'platform':platform.system(),'architecture':platform.machine(),'components':components,'ready':all(components.values()),'optional_ssh_available':shutil.which('ssh') is not None,'notice':'Checks local prerequisites only. No TTS model, provider credentials, game client or network connection is tested.'}

def check_deps():
 report=dependency_report();missing=[name for name,available in report['components'].items() if not available]
 if missing:
  print('Missing prerequisites: '+', '.join(missing));print('See SETUP.txt. Install Python 3.11+, then use this interpreter:');print('"'+sys.executable+'" -m pip install -r "'+str(ROOT/'requirements.txt')+'"');print('ffmpeg and ffprobe must also be in PATH. Nothing was installed automatically.');raise SystemExit(1)
 return report

def alignment_diagnostics(folder):
 from configparser import ConfigParser, Error
 from lite_alignment import alignment_profile
 parser=ConfigParser(interpolation=None)
 config=Path(folder).expanduser().resolve()/'providers.ini' if folder else ROOT/'providers.ini'
 try:
  parser.read(config,encoding='utf-8-sig')
  profile=alignment_profile(parser)
 except Error:
  return {'ready':False,'detail':'The local provider configuration is unreadable.'}
 except (OSError,ValueError) as error:
  return {'ready':False,'detail':str(error)}
 return {'ready':True,'profile_id':profile['profile_id']}

def ensure_config(folder=None):
 target=(Path(folder).expanduser().resolve() if folder else ROOT)/'providers.ini'
 if target.is_file():return target
 template=ROOT/'providers.example.ini'
 if not template.is_file():raise ValueError('providers.example.ini is missing. Extract the complete source package.')
 try:
  target.parent.mkdir(parents=True,exist_ok=True)
  with target.open('xb') as output:output.write(template.read_bytes())
 except FileExistsError:pass
 except OSError as error:raise ValueError('Choose a writable DMSpeaks data folder for providers.ini.') from error
 return target

def open_browser(port):
 url=f'http://127.0.0.1:{port}/'
 def open_page():
  try:
   if not webbrowser.open(url):print('Open this address in a browser: '+url,flush=True)
  except Exception:print('Open this address in a browser: '+url,flush=True)
 threading.Thread(target=open_page,daemon=True).start()

def main():
 parser=argparse.ArgumentParser(description='DMSpeaks local HTML speech editor')
 parser.add_argument('--port',type=int,default=8770);parser.add_argument('--data',default=None)
 parser.add_argument('--no-browser',action='store_true');parser.add_argument('--check',action='store_true',help='Check local prerequisites without starting a server or modifying files')
 parser.add_argument('--diagnostics',action='store_true',help='Print local prerequisite information as JSON; no keys or network checks')
 parser.add_argument('--wow-root',help='WoW client folder containing Interface and WTF. Wait in the background for in-game menu requests.')
 parser.add_argument('--plans',help='Prepared dialogue catalog for the WoW background companion.')
 parser.add_argument('--plans-sha256',help='SHA-256 supplied with the prepared dialogue catalog.')
 parser.add_argument('--paced-deliveries',help='Companion paced playback collection to verify before fetching.')
 parser.add_argument('--paced-deliveries-sha256',help='SHA-256 supplied with the paced collection archive.')
 parser.add_argument('--paced-input-sha256',help='External SHA-256 of the paced collection input index.')
 args=parser.parse_args()
 if not 1<=args.port<=65535:parser.error('Port must be between 1 and 65535.')
 if bool(args.plans)!=bool(args.plans_sha256) or (args.plans and not args.wow_root):
  parser.error('--plans and --plans-sha256 require each other and --wow-root.')
 paced=(args.paced_deliveries,args.paced_deliveries_sha256,args.paced_input_sha256)
 if any(paced) and (not all(paced) or not args.wow_root):
  parser.error('The three paced collection arguments require each other and --wow-root.')
 if args.diagnostics:
  report=dependency_report()
  if report['ready']:report['player_name_alignment']=alignment_diagnostics(args.data)
  print(json.dumps(report,indent=2));return 0 if report['ready'] else 1
 report=check_deps()
 if args.check:print('Local prerequisites are available. No model or provider was contacted.');return 0
 try:
  folder=Path(args.data).expanduser().resolve() if args.data else None
  connection_file=ensure_config(folder);import server
  server.serve(port=args.port,folder=folder,on_ready=None if args.no_browser or args.wow_root else open_browser,
               connection_file=connection_file,wow_root=args.wow_root,
               game_plans=args.plans,game_plans_sha256=args.plans_sha256,
               game_paced=dict(path=args.paced_deliveries,sha256=args.paced_deliveries_sha256,
                               input_sha256=args.paced_input_sha256) if all(paced) else None)
 except KeyboardInterrupt:print('\nDMSpeaks stopped.');return 0
 except (ImportError,OSError,ValueError) as error:
  print('Could not start DMSpeaks: '+str(error));print('If the port is already in use, close the other companion or choose --port NUMBER. See SETUP.txt.');return 1
 return 0
if __name__=='__main__':sys.exit(main())
