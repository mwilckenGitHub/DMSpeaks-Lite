"""Bake per-candidate inherited effects. Original media stays immutable."""
import hashlib
import shutil
from pathlib import Path

import clip_group_schema
import audio
import audio_effects


def copy_processed_clip(source, target, group, asset_id):
    src = Path(source).resolve()
    tgt = Path(target).resolve()
    if src == tgt:
        raise ValueError("source and target resolve to the same path")
    if not src.is_file():
        raise FileNotFoundError(f"source not found: {src}")
    if tgt.exists():
        raise ValueError('Choose a new output path for processed audio.')

    chain = clip_group_schema.effective_effects(group, asset_id) if group is not None else []
    section_chain=clip_group_schema.effective_section_effects(group,asset_id) if group is not None else []
    if not isinstance(chain, list):
        raise ValueError("malformed group effect chain")

    def _sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    src_hash_before = _sha256(src)
    source_info=audio.probe(src)
    if source_info['channels'] not in (1,2):raise ValueError('Use mono or stereo candidate audio.')
    tgt.parent.mkdir(parents=True, exist_ok=True)
    try:
        enabled = [n for n in chain+section_chain if n.get("enabled")]
        if enabled or source_info['codec']!='vorbis':
            ch = source_info['channels']
            pcm = audio.decode(src, channels=ch)
            pcm, info = audio_effects.apply_effects(pcm, ch, chain)
            pcm,section_info=audio_effects.apply_effects(pcm,ch,section_chain)
            info['effect_tail_sample_count']+=section_info['effect_tail_sample_count']
            peak = max(abs(x) for x in pcm) if pcm else 0.0
            if peak > 1.0:
                raise ValueError(
                    f"peak {peak:.6f} exceeds 1.0; lower effect levels to avoid clipping"
                )
            audio.encode_pcm(pcm, tgt, channels=ch)
            effects_engine = info.get("effects_engine", "unknown") if enabled else 'whole-source conversion to Ogg Vorbis; no active effects'
            dry = info.get("dry_rendered_sample_count", 0)
            tail = info.get("effect_tail_sample_count", 0)
        else:
            shutil.copyfile(src, tgt)
            effects_engine = "unchanged source copy"
            dry = None
            tail = 0

        src_hash_after = _sha256(src)
        if src_hash_before != src_hash_after:
            raise ValueError("source hash changed during processing")

        return {
            "format": "dmspeaks-clip-processing",
            "version": "0.1",
            "group_id": group.get("id") if isinstance(group, dict) else None,
            "effects_baked": True,
            "effects": chain,
            "section_effects": section_chain,
            "effects_engine": effects_engine,
            "dry_rendered_sample_count": dry,
            "effect_tail_sample_count": tail,
            "source_sha256": src_hash_before,
        }
    except Exception:
        if tgt.exists():
            tgt.unlink(missing_ok=True)
        raise
