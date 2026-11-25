"""Separate physical stage directions from sounds made by the speaking character.

Classification is deliberately conservative, not a natural-language understanding
guarantee. Unknown/compound actions stay silent pending review. Original source is
never rewritten. Narration is a separate clip role, never an NPC performance tag.
"""
import hashlib,re
CONVERSION_VERSION=2
EMOTE=re.compile(r'<([^<>\n]+)>')
XML_TAG=re.compile(r'^/?(?:speak|s|p|break|prosody|voice|emphasis|phoneme|sub|say-as|audio|mark|lang|lexicon|lookup|w|token|mstts:[\w-]+|amazon:[\w-]+)(?:\s[^<>]*|\s*/)?$',re.I)

# Only bare sounds or simple named-actor actions are converted automatically.
# The short Fish tag is an acting request; its acoustic realization needs review.
VOCAL_TAGS={'laugh':'laughing','laughs':'laughing','laughing':'laughing',
 'chuckle':'chuckling','chuckles':'chuckling','sigh':'sighing','sighs':'sighing',
 'cough':'coughing','coughs':'coughing','hic':'hiccup','hiccups':'hiccup',
 'sob':'sobbing','sobs':'sobbing','weeps':'crying','cry':'crying','cries':'crying',
 'growls':'growling','grunts':'grunting','gasps':'gasping','hums':'humming'}
INTONATION_TAGS={'whispers':'whisper','whisper':'whisper','shouts':'shouting','mutters':'muttering'}
NAMED_ACTION=re.compile(r"^(?:(?:[A-Z][\w'’\-]*|the|The)(?:\s+(?:[A-Z][\w'’\-]*|the|The))*\s+)?([a-z]+)[.!]?$",re.UNICODE)
VISUAL_ACTION=re.compile(r'\b(?:looks?|nods?|points?|shrugs?|gestures?|waves?|bows?|kneels?|walks?|runs?|turns?|opens?|closes?|hands?|reaches?|stares?|smiles?|frowns?|grins?|steps?|shakes?|raises?|lowers?|sits?|stands?|salutes?)\b',re.I)

def classify_emote(cue):
 match=NAMED_ACTION.fullmatch(cue.strip())
 action=match[1] if match else None
 if action in VOCAL_TAGS:return 'vocal','['+VOCAL_TAGS[action]+']'
 if action in INTONATION_TAGS:return 'intonation','['+INTONATION_TAGS[action]+']'
 # Mixed visual/vocal clauses require author review rather than guessing.
 if VISUAL_ACTION.search(cue) and not re.search(r'\b(?:'+ '|'.join(VOCAL_TAGS|INTONATION_TAGS)+r')\b',cue,re.I):return 'visual',''
 return 'unknown',''

def emote_metadata(text, *, ignored_spans=()):
 """Local draft extension; Unicode source offsets are half-open codepoint spans."""
 events=[]
 for match in EMOTE.finditer(text):
  if any(start<match.end() and end>match.start() for start,end in ignored_spans):continue
  cue=match[1]
  if XML_TAG.fullmatch(cue.strip()):continue
  if '[' in cue or ']' in cue:raise ValueError('Finish the nested direction markup before generating speech.')
  kind,tag=classify_emote(cue)
  ident='emote-'+hashlib.sha256((str(match.start())+'\0'+match[0]).encode()).hexdigest()[:16]
  events.append({'event_id':ident,'kind':kind,'original_markup':match[0],
   'source_start':match.start(),'source_end':match.end(),
   'speaker_role':'npc' if kind in ('vocal','intonation') else 'narrator' if kind=='visual' else None,
   'tts_markup':tag,'spoken':False,'review_required':kind=='unknown',
   'narration':{'eligible':kind=='visual','selected':False,'text':cue if kind=='visual' else '',
    'clip_role':'visual_narration','speaker_ref':None,'clips':{}}})
 return {'format':'dmspeaks.performance_events','version':'0.1',
  'conversion_version':CONVERSION_VERSION,'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
  'coordinates':'unicode_codepoints_half_open','classification':'conservative_rules_requires_review',
  'narrator':{'enabled':False,'voice_selection':'off','voice_choices':['male','female'],
   'voice_references':{'male':None,'female':None},'missing_voice_policy':'skip_narration',
   'scope':'visual_narration_only'},'events':events}

def convert_wow_emotes(text):
 events=emote_metadata(text)['events'];by_start={e['source_start']:e for e in events};delta=0
 def replace(match):
  nonlocal delta
  # Imported SSML can coexist with game text. Do not rewrite actual XML elements.
  if match.start() not in by_start:return match[0]
  event=by_start[match.start()];direction=event['tts_markup']
  # A space avoids joining words where a silent stage action was inline.
  if not direction:direction=' '
  event['adapter_start']=match.start()+delta;event['adapter_end']=event['adapter_start']+len(direction)
  event['tts_markup']=direction;delta+=len(direction)-len(match[0])
  return direction
 return EMOTE.sub(replace,text),events

def stage_format(record,request):
 explicit=request.get('stage_directions')
 if explicit not in (None,'auto'):
  if explicit not in ('wow','preserve'):raise ValueError('Choose auto, wow or preserve for stage directions.')
  return explicit
 fields=record.get('all_metadata',{})
 declared=fields.get('source.stage_direction_format')
 if declared in ('wow','preserve'):return declared
 # The checked Spoken corpus keeps originalText and a native fileName on each row.
 return 'wow' if (record.get('source_text_field')=='originalText' or 'originalText' in fields) and 'fileName' in fields else 'preserve'
