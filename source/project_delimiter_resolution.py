"""Resolve project and dialogue delimiter definitions without mutating either."""
from delimiter_definitions import LIMIT, validate_definition


def resolve(project_definitions, dialogue_definitions):
    """Return effective authored definitions; a matching local key overrides.

    A collision between different keys is ambiguous and is reported rather than
    silently changing which placeholder will be rendered. Inferred source
    variables are intentionally outside this function's scope.
    """
    if not isinstance(project_definitions, list) or not isinstance(dialogue_definitions, list):
        raise ValueError('Delimiter collections must be lists.')
    if len(project_definitions) > LIMIT or len(dialogue_definitions) > LIMIT:
        raise ValueError('Too many delimiters.')

    def normalized(collection):
        result = [validate_definition(item) for item in collection]
        for field in ('key', 'name', 'representation'):
            values = [item[field].casefold() if field == 'name' else item[field] for item in result]
            if len(values) != len(set(values)):
                raise ValueError(f'Duplicate delimiter {field} in one collection.')
        return result

    inherited = normalized(project_definitions)
    local = normalized(dialogue_definitions)
    local_by_key = {item['key']: item for item in local}
    project_keys = {item['key'] for item in inherited}
    effective = []
    for item in inherited:
        definition = local_by_key.get(item['key'], item)
        if (definition.get('type', 'replacement') == 'replacement' and
                'snap_mode' in item and 'snap_mode' not in definition):
            definition = {**definition, 'snap_mode': item['snap_mode']}
        effective.append(definition)
    effective.extend(item for item in local if item['key'] not in project_keys)
    if len(effective) > LIMIT:
        raise ValueError('Too many effective delimiters.')
    for field in ('name', 'representation'):
        owners = {}
        for item in effective:
            value = item[field].casefold() if field == 'name' else item[field]
            if value in owners and owners[value] != item['key']:
                raise ValueError(f'Conflicting delimiter {field}: {item[field]}')
            owners[value] = item['key']
    return effective
