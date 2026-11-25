"""CSV writer drafted by the established local Qwen worker and reviewed before integration."""
import csv,json

def write_arrangement_csv(path, layers, clock_rate, arrangement_id, *, track_buses=()):
    if not isinstance(clock_rate, int) or isinstance(clock_rate, bool) or clock_rate <= 0:
        raise ValueError("clock_rate must be a positive integer")

    headers = [
        "arrangement_id", "arrangement_sample_rate", "layer_id", "layer_track_id",
        "layer_order", "layer_after_id", "layer_start_sample", "layer_end_sample",
        "layer_sample_count", "layer_start_seconds", "layer_end_seconds",
        "layer_audio_path", "layer_audio_sha256", "layer_export_status",
        "layer_dry_sample_count", "layer_effect_tail_sample_count", "layer_effects_baked_json",
        "layer_clip_role", "layer_narrator_label", "layer_narration_source_link_json",
        "layer_source_audio_path", "layer_source_audio_sha256", "layer_source_bytes_preserved",
        "layer_source_mute_windows_json", "layer_source_mute_clock_rate",
        "layer_kind", "layer_label", "layer_source_asset_id", "layer_placeholder",
        "layer_selected_value", "layer_slot_json", "row_type", "layer_track_bus_id", "bus_member_layer_ids_json",
        "layer_background_loop_json", "layer_ducking_json", "layer_ducking_applied",
        "layer_guide_binding_json", "layer_guide_binding_resolution_json", "layer_snap_points_json"
    ]

    def get_val(d, key):
        v = d.get(key)
        return "" if v is None else v

    def to_seconds(samples):
        if samples is None:
            return ""
        return samples / clock_rate

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for layer in [*layers,*track_buses]:
            row = {
                "arrangement_id": arrangement_id,
                "arrangement_sample_rate": clock_rate,
                "layer_id": get_val(layer, "id"),
                "layer_track_id": get_val(layer, "track_id"),
                "layer_order": get_val(layer, "slot_order"),
                "layer_after_id": get_val(layer, "after_layer_id"),
                "layer_start_sample": get_val(layer, "start_sample"),
                "layer_end_sample": get_val(layer, "end_sample"),
                "layer_sample_count": get_val(layer, "rendered_sample_count"),
                "layer_start_seconds": to_seconds(layer.get("start_sample")),
                "layer_end_seconds": to_seconds(layer.get("end_sample")),
                "layer_audio_path": get_val(layer, "audio_path"),
                "layer_audio_sha256": get_val(layer, "audio_sha256"),
                "layer_export_status": get_val(layer, "export_status"),
                "layer_dry_sample_count": get_val(layer,"dry_rendered_sample_count"),
                "layer_effect_tail_sample_count": get_val(layer,"effect_tail_sample_count"),
                "layer_effects_baked_json": json.dumps(layer.get("effects",[]),separators=(",",":")),
                "layer_clip_role": get_val(layer,"clip_role"),
                "layer_narrator_label": get_val(layer,"narrator_label"),
                "layer_narration_source_link_json": json.dumps(layer.get("narration_source_link",{}),separators=(",",":")),
                "layer_source_audio_path": get_val(layer,"source_audio_path"),
                "layer_source_audio_sha256": get_val(layer,"source_audio_sha256"),
                "layer_source_bytes_preserved": get_val(layer,"source_bytes_preserved"),
                "layer_source_mute_windows_json": json.dumps(layer.get('source_mute_windows',[]),separators=(',',':')),
                "layer_source_mute_clock_rate": get_val(layer,'source_mute_clock_rate'),
                "layer_kind": get_val(layer,'kind'),
                "layer_label": get_val(layer,'label'),
                "layer_source_asset_id": get_val(layer,'asset_id'),
                "layer_placeholder": get_val(layer.get('slot',{}),'placeholder'),
                "layer_selected_value": get_val(layer.get('slot',{}),'value'),
                "layer_slot_json": json.dumps(layer.get('slot',{}),ensure_ascii=False,separators=(',',':')),
                "row_type": 'track_bus' if layer.get('kind')=='track_bus' else 'bus_member' if layer.get('track_bus_id') else 'clip',
                "layer_track_bus_id": layer.get('track_bus_id') or (layer['id'] if layer.get('kind')=='track_bus' else ''),
                "bus_member_layer_ids_json": json.dumps(layer.get('member_layer_ids',[]),separators=(',',':')),
                "layer_background_loop_json": json.dumps(layer.get('background_loop'),separators=(',',':')),
                "layer_ducking_json": json.dumps(layer.get('ducking'),separators=(',',':')),
                "layer_ducking_applied": get_val(layer,'ducking_applied'),
                "layer_guide_binding_json": json.dumps(layer.get('guide_binding'),separators=(',',':')),
                "layer_guide_binding_resolution_json": json.dumps(layer.get('guide_binding_resolution'),separators=(',',':')),
                "layer_snap_points_json": json.dumps(layer.get('snap_points'),separators=(',',':'))
            }
            writer.writerow(row)
