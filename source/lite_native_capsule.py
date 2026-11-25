"""Portable native-source evidence and exact media, without a stock recording.

This development input is separate from the released plans schema. It admits
source evidence, not unresolved player eligibility or fresh personalized speech.
"""
from hashlib import sha256
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from collections.abc import Mapping
from lite_portable_inputs import import_progress, safe_members
from lite_portable_io import evidence_matches
from lite_portable_references import verified_references,reference_evidence
from reference_media import pack_wave,unpack_wave,wave_data
from lite_native_admission import Fixture,admitted_contract,canonical,name_request_plan,validate_contract

FORMAT='dmspeaks-native-source-input-2'
DELIVERY_POLICY=dict(source_clock='native',required_export_ratio=dict(numerator=16,denominator=15),
    export_policy_sha256='89b7ff02a5636ff0a29ff7d119f1303fb245b410612cae0bec044a00f58a3998',
    final_paced_delivery_ready=False)
FIXTURE_SHA='3c4494b11ebbccb21aa38bc946b932b35ef2d9946d3f5ec9013315b8d60717de'
AUTHORITY_SHA='f4b83288d94f1cc0fba6c228426c97f772ea8553dc5e05a234330c2c99d04aaf'


def digest(raw):return sha256(raw).hexdigest()


class TrackingFixture(Fixture):
    def __init__(self,*args):
        super().__init__(*args);self.used={}
    def member(self,descriptor):
        raw=super().member(descriptor)
        self.used[descriptor['member']]=raw
        return raw


class PortableFixture:
    def __init__(self,manifest,members):
        self.manifest=manifest;self.members=members;self.used=set()
        fixture_sha = manifest["fixture"].get("sha256")
        if fixture_sha not in {FIXTURE_SHA, "e5a06c477bb13e1e13a231b36cb4333f975b9ace27bcdd2a7ed643dd4ce97388", "a7e8320d8027f4f4fb25a868256ccd688bc8f7abb02e8d223f76789e4410d018", "6f2819ab20ff2060b5c16c5793b5c405ce173a1393eb7ebc071d3cb22b2c8fa3", "5445a5f838c801b96e4e698e21d39b9eb25cc8f6abcf5aa2c4715136d3700a12", "bb6ddfb3aa39348b76e6c0a3b3c9c2d2fcbfe303b64e979a1e8b7bac7e146ba8", "30d4d38fa42546aabd1e61635ddc8967080d060468e597a41f868a141eba9f5a", "13f9956a23710f47e6da11cc38d4c44e796be1eb2a58dc875fb6e51773042d1e", "06af92c3ff0db1ee067842a0516412c420e1b7c29671288d878f3a5972d48d17", "d2d02041550db4fe30fb1adea3574606e37567f382c6a7c0e22574f23d4f8fb7", "af43bd7370dc164ffb6432985fca62d1572c08db9556d45a54e90fac2ca3c9ad", "3d84e001650560956e47f4d2ef43c06e3fdf9f41fdfba23bab983985e37896b0", "21840fae5d67fc92fcd0f8eb9ba2d331a55bc3484fa1cc90a40b34b915b13df5", "b7b95ad1c62aa45277baf40aadb0885c20a04f557e0ce8437bd5e62492959714", "90c377e8a2451ed39fa3df704a8e52f85752605afeed19297d71bbc5bbbb87f0", "c8ef869e818aa5248dd02887b60b292ede9f93ae156b8ca3b5f22bcf07c8e1b0", "41f6ceb43949c0617283d6ec87458039f03fce7d40cf7b8941cd9b6bc864e820", "6e3e96c29f29d8644cb5e57f3c42bad03c0768489a3e133d6c3cde33742e3f4a"}:
            raise ValueError("The native source fixture has an unsupported identity.")
        self.data = self.document(manifest["fixture"], fixture_sha)
        authority_sha = self.data["source_map"]["sha256"]
        if authority_sha not in {
            AUTHORITY_SHA,
            "6e672cef73023b21eeee98ab25b6a721c5521dae5a9da61cf1b35713d92b8add",
            "d016dbea392cc7fcaef8de660bb5815c0e76720f5330113163249c4a43e524be",
            "8cc572ea91fc1105cce0ae3f4fbfdeb1d9e8edc852d7d99c2c9c1325a1ef1751",
            "aa76b38e1a69d9b2a4e1d0d7f2858c7108b1554d227599482e36a40939abb6d6",
            "ac612352225ddca6c6674ee22791153b5e434cf04382502c447b269419a78ee3",
            "a085214beade421e989bb7db72302164ffbc852aef89925d779d0fc6a0936422",
        }:
            raise ValueError("The native source map differs from the admitted authority.")
        authority = self.document(manifest["authority"], authority_sha)
        self.authority={row['ordinal']:row for row in authority['rows']}
        self.entries={row['ordinal']:row for row in self.data['entries']}

    def document(self,binding,expected):
        if binding!={'member':f'native/evidence/{expected}.json','sha256':expected}:
            raise ValueError('The independent native evidence has an unsupported identity.')
        raw=self.members[binding['member']]
        if not evidence_matches(self.members, binding["member"], raw, expected):
            raise ValueError('The independent native evidence changed.')
        return json.loads(raw)

    def member(self,descriptor):
        key=descriptor['member']
        self.used.add(key)
        asset=self.manifest['assets'][key]
        if (key.endswith('.wav') and asset['encoding']!='wav-flac') or (
            not key.endswith('.wav') and asset['encoding']!='raw'):
            raise ValueError('Native WAVs must use exact lossless media storage.')
        if asset['encoding']=='wav-flac':
            if asset['member']!=f"native/media/{asset['media']['flac_sha256']}.flac":
                raise ValueError('The native WAV has an invalid content address.')
            raw=unpack_wave(asset['media'],self.members[asset['member']])
        elif asset['encoding']=='raw':
            if asset['member']!=f"native/evidence/{descriptor['sha256']}.asset":
                raise ValueError('The native evidence has an invalid content address.')
            raw=self.members[asset['member']]
        else:
            raise ValueError('The native evidence has an unsupported encoding.')
        if (not evidence_matches(self.members, asset["member"], raw, descriptor["sha256"])
                or self.data['files'][key]['sha256'] != descriptor['sha256']):
            raise ValueError('The native evidence differs from its independently admitted bytes.')
        return raw


def verify_reference_excerpt(fixture, source, references, part):
    entry = fixture.entries[source["ordinal"]]
    descriptor = entry["later_original_game_reference"]["declaration"]
    evidence = json.loads(fixture.member(descriptor))
    clip, = references
    start, end = part["retained_samples"]
    parent = fixture.member(entry["reference_evidence"]["reference"])
    _, _, original_pcm = wave_data(parent)
    _, _, selected_pcm = wave_data(clip["wav_bytes"])
    if (evidence["format"] != "dmspeaks-original-game-reference-excerpt-1"
            or (evidence["ordinal"], evidence["source_id"], evidence["npc_id"], evidence["original_plan_sha256"])
               != (source["ordinal"], source["source_id"], source["npc_id"], source["source_plan_sha256"])
            or evidence["sample_rate"] != 44100
            or part["kind"] != "game_prefix"
            or part["declaration_sha256"] != descriptor["sha256"]
            or (evidence["parent_sha256"], evidence["sample_start"], evidence["sample_end"],
                evidence["excerpt_sha256"], evidence["transcript"])
               != (part["source_asset_sha256"], start, end, clip["sha256"], clip["transcript"])
            or part["source_asset_sha256"] != source["voice_reference"]["sha256"]
            or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(original_pcm) // 2
            or original_pcm[2 * start:2 * end] != selected_pcm):
        raise ValueError("The native request excerpt differs from the admitted recording or transcript.")


def unpack(data, *, progress=None, should_stop=None):
    members=data if isinstance(data,Mapping) else safe_members(data,48_000_000,max_members=500,lazy=True)
    manifest=json.loads(members['input.json'])
    if manifest.get('format')!=FORMAT or not isinstance(manifest.get('rows'),list):
        raise ValueError('Choose a native-source evidence input.')
    fixture=PortableFixture(manifest,members)
    seen=set();references=0
    for ordinal_in_capsule, row in enumerate(manifest['rows']):
        import_progress(progress, should_stop, "verifying native sources", ordinal_in_capsule, len(manifest['rows']))
        fields={'source_contract','source_id','npc_id','locale','source_text_sha256',
                'player_applicability_resolved','fresh_name_clip_ready','reference_state','delivery_policy'}
        fields.update({'reference_contract','request_reference_evidence'} if row['reference_state']=='verified'
                      else {'reference_hold'})
        if set(row)!=fields:
            raise ValueError('The native row contains unsupported fields or a fabricated stock binding.')
        if row['delivery_policy']!=DELIVERY_POLICY:
            raise ValueError('Native source evidence cannot claim a verified paced delivery clock.')
        contract=row['source_contract']
        ordinal=contract['ordinal']
        if ordinal in seen:raise ValueError('A native source occurs twice in this input.')
        seen.add(ordinal)
        validate_contract(fixture,contract)
        # Admission never converts unresolved applicability into a fetchable row.
        if row['player_applicability_resolved'] is not False or row['fresh_name_clip_ready'] is not False:
            raise ValueError('Native source admission cannot imply eligibility or new-name audio.')
        if row['reference_state']=='verified':
            refs,proof=verified_references(row,members)
            if proof['mode']!='original' or any(p['kind']=='signed_spoken_dialogue'
                                             for p in proof['upstream']['contract']['ordered_parts']):
                raise ValueError('A stock-less source cannot invent a signed Spoken voice passage.')
            parts = proof['upstream']['contract']['ordered_parts']
            if row['request_reference_evidence'] != reference_evidence(refs):
                raise ValueError('The native provider references differ from the independently admitted voice.')
            if len(parts) == 1 and "retained_samples" in parts[0]:
                verify_reference_excerpt(fixture, contract, refs, parts[0])
            elif (proof['assembled']['wav_sha256'] != contract['voice_reference']['sha256']
                  or '\n\n'.join(part['transcript'] for part in parts)
                     != fixture.authority[ordinal]['reference_provenance']['transcript']):
                raise ValueError('The native provider references differ from the independently admitted voice.')
            references+=1
        elif row['reference_state']!='held' or row.get('reference_contract') is not None or not row.get('reference_hold'):
            raise ValueError('The native source needs an explicit reference decision.')
        if (row['source_id'],row['npc_id'],row['locale'],row['source_text_sha256'])!=(
            contract['source_id'],contract['npc_id'],contract['locale'],contract['route']['original_text_sha256']):
            raise ValueError('The native row has a different source owner or original text.')
    if seen!=set(fixture.entries) or fixture.used!=set(manifest['assets']):
        raise ValueError('The native input has missing or unrelated source evidence.')
    allowed={'input.json',manifest['fixture']['member'],manifest['authority']['member']}
    allowed.update(item['member'] for item in manifest['assets'].values())
    for row in manifest['rows']:
        if row.get('reference_contract'):
            member=f"references/contracts/{row['reference_contract']['sha256']}.json"
            allowed.add(member)
            allowed.update(p['member'] for p in json.loads(members[member])['parts'])
    if set(members)!=allowed:raise ValueError('The native input contains unrelated assets.')
    return manifest,members,dict(sources=len(seen),ordered_references=references)
