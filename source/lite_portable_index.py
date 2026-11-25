"""Render personalized dialogue routes from the companion manifest."""

from hashlib import sha256

from guide_bindings import RATE
from lite_portable_io import manifest_render_fingerprint, stock_binding
from spoken_pack_lua import lua_string

# Forever 1.60.1.70938 gives Aurora's existing display 9754 a new model
# file and keeps greeting kit 7014. The campaign route still uses its old
# Spoken key. Bind the extra model only to that exact speaker and line.
FOLLOWUP_MODELS = {
    ("f:8338:model-312", 10304, 8338, 124368,
     "7e8743e8d51ba0d2bd1133f721a3a76cc0d2e018671226a1fb47b21c58a25370"): (1100258,),
}

# The exact conditional branch of Itharius's gossip menu uses condition 1341.
GOSSIP_ITEMS = {
    ("g:89defc6255de548061e4c860609dfe75", 5353,
     "fe08717a20aa3cef1459a2e768082243ddb9775a3c2f638690b79caf291debd0"): (10455, 1),
}


def quote_path(path):
    return lua_string(str(path).replace("/", "\\"))


def render_index_entry(manifest, folder, stock_path, *, names=None, store_seal=None):
    def clip(item):
        article = item.get("name_article")
        caption_article = (f", articleFrom = {lua_string(article['source'])}, "
                           f"articleTo = {lua_string(article['spoken_text'])}" if article else "")
        return f"{{ path = {quote_path(folder / item['file'])}, samples = {item['samples']}{caption_article} }}"

    def sequence(route):
        parts = []
        for index, part in enumerate(route, 1):
            first, last = part['source_samples']
            fields = [f'first = {first}', f'last = {last}']
            if part['kind'] == 'fixed':
                fields.append(f"fixed = {clip(part['clip'])}")
            else:
                choices = ', '.join(f'[{lua_string(key)}] = {clip(item)}'
                                    for key, item in part['choices'].items())
                fields.extend((f"kind = {lua_string(part['kind'])}",
                               f"fallback = {lua_string(part['fallback'])}",
                               f'choices = {{ {choices} }}'))
                if store_seal and part["kind"] == "name":
                    from lite_store_playback import slot_key
                    fields.append(f"nameSlot = {lua_string(slot_key(store_seal, index))}")
                if part['kind'] == 'gender':
                    values = ', '.join(f'[{int(sex)}] = {lua_string(key)}'
                                       for sex, key in part['value_by_player_sex'].items())
                    fields.append(f'bySex = {{ {values} }}')
                if binding:=part.get('caption_binding'):
                    choices=', '.join(f'[{lua_string(key)}] = {lua_string(value)}' for key,value in binding['choices'].items())
                    measured = ''
                    if starts := binding.get('delivered_word_samples'):
                        rows = ', '.join(f'[{lua_string(key)}] = {{ {", ".join(map(str, samples))} }}'
                                         for key, samples in starts.items())
                        measured = f', deliveredStarts = {{ {rows} }}'
                    fields.append(f"caption = {{ first = {binding['first']}, last = {binding['last']}, source = {lua_string(binding['source'])}, choices = {{ {choices} }}{measured} }}")
            parts.append('        { ' + ', '.join(fields) + ' },')
        return '{\n' + '\n'.join(parts) + '\n    }'

    stock = quote_path('Interface/AddOns/' + stock_path) if stock_path else lua_string(manifest['spoken_key'])
    binding = stock_binding(manifest) if stock_path else None
    identity = (f"stockSha256 = {lua_string(binding['sha256'])}, "
                f"renderSha256 = {lua_string(manifest_render_fingerprint(manifest))}, "
                if binding else "")
    default = sequence(manifest['sequence'])
    routes = manifest.get('character_sequences', {})
    by_name = ''
    if routes:
        choices = ',\n'.join(f'[{lua_string(name)}] = {sequence(route)}'
                             for name, route in sorted(routes.items()))
        by_name = ', byName = {\n' + choices + '\n    }'
    if names:
        by_name += ', names = { ' + ', '.join(f'[{lua_string(name)}] = true' for name in sorted(names)) + ' }'
    if store_seal:
        by_name += f", storeRevision = {lua_string(store_seal['clock_contract_sha256'])}"
    if caption := manifest.get('reconstructed_caption'):
        timings = ', '.join(f'{sample / RATE:.12f}' for sample in caption['word_samples'])
        by_name += (f", baseText = {lua_string(caption['text'])}, baseTimings = {{ {timings} }}, "
                    f"npc = {manifest['npc_id']}, event = {caption['event']}, text = {lua_string(caption['quest_text'])}")
    if route:=manifest.get('native_route'):
        if manifest.get('delivery_clock_format'):
            by_name+=f", deliveryClock = {lua_string(manifest['delivery_clock_format'])}, captionPolicy = {lua_string(manifest['caption_policy'])}"
        filters=manifest.get('native_character_filters',{})
        for source,target in (('races','allowedRaces'),('classes','allowedClasses')):
            if source in filters:
                by_name+=', '+target+' = { '+', '.join(f'[{lua_string(value)}] = true' for value in filters[source])+' }'
        if filters.get('sexes'):
            by_name+=', allowedSex = { '+', '.join(f'[{2 if value=="male" else 3}] = true' for value in filters['sexes'])+' }'
        if route['kind']=='gossip':
            by_name+=(f", giverType = {lua_string(route['giver_type'])}, text = {lua_string(route['original_text'])}")
            identity = (manifest["source_id"], route["npc_id"], sha256(route["original_text"].encode()).hexdigest())
            if requirement := GOSSIP_ITEMS.get(identity):
                item, count = requirement
                by_name += f", gossipInventory = {{ item = {item}, count = {count} }}"
            branch = manifest['source_id'].rsplit(':', 1)[-1]
            if branch in ('m', 'f'):
                by_name += f", playerSex = {2 if branch == 'm' else 3}"
        elif route['kind']=='followup':
            if not manifest.get('reconstructed_caption'):
                by_name+=f", npc = {route['npc_id']}, event = 6, text = {lua_string(route['original_text'])}"
            if "model_file_id" in route:
                by_name+=f", modelFileID = {route['model_file_id']}"
                identity = (manifest["source_id"], route["npc_id"], route.get("broadcast_id"),
                            route["model_file_id"], sha256(route["original_text"].encode()).hexdigest())
                if aliases := FOLLOWUP_MODELS.get(identity):
                    models = ", ".join(f"[{file_id}] = true" for file_id in aliases)
                    by_name += f", verifiedModelAliases = {{ {models} }}"
        elif route['kind']=='quest':
            if not manifest.get('reconstructed_caption'):
                event={'accept':1,'progress':2,'complete':3}[route['event']]
                by_name+=f", npc = {route['npc_id']}, event = {event}, text = {lua_string(route['original_text'])}"
            giver_flag = {"Item": "itemGiver", "GameObject": "objectGiver"}.get(route.get('giver_type'))
            if giver_flag and "quest_id" in route:
                by_name += f", {giver_flag} = true, questID = {route['quest_id']}"
            if variant := route.get('verified_quest_stock_variant'):
                by_name += f", verifiedStockPath = {quote_path('Interface/AddOns/' + variant['path'])}"
            if aliases := route.get('verified_quest_giver_aliases'):
                by_name += ', questGiverAliases = { ' + ', '.join(f'[{npc}] = true' for npc in aliases) + ' }'
        else:
            raise ValueError('The native route needs an explicit event adapter.')
        return (f"    {{ key = {lua_string(manifest['spoken_key'])}, rate = {RATE}, "
                f"sequence = {default}{by_name} }},")
    return (f"    [{stock}] = {{ {identity}key = {lua_string(manifest['spoken_key'])}, rate = {RATE}, "
            f"sequence = {default}{by_name} }},")
