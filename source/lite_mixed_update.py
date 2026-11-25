"""Rebuild a mixed companion from verified inputs before a coverage update."""
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from lite_portable_io import read, file_hash, digest, filesystem_path
from lite_portable_package import INDEX, package_names


def rebuild_update(app, addons, previous, incoming, output):
    """Return a private mixed archive only if it accounts for the live index.

    Installed manifests are never used as trust roots. Reconstruct the prior
    index from the imported plan, checked stock audio and externally sealed
    paced inputs, then compare it byte-for-byte with the installed index.
    """
    from lite_portable_inputs import SETTING, imported, installed, reconstructed_caption
    from lite_portable_companion import installed_companion
    from lite_source_identity import source_key, manifest_key, native_identity
    from lite_paced_delivery import canonical, validate_projection
    plan, members = imported(app, 'plans')
    config = app.store.setting(SETTING, {})['plans']
    work = app.folder / 'spoken-player-names'
    packages = work / 'packages' / config['sha256']
    current = {source_key(row): row for row in plan['dialogues']}
    index = addons / INDEX
    expected = file_hash(index) if index.is_file() else None

    def build(state, archive, *, legacy_playback_paths=False, legacy_addons=None):
        covered = {}
        for path, seal in state.get('seals', {}).items():
            folder = filesystem_path(path)
            validate_projection(folder, read(folder/'dialogue.json'), seal)
            extension = read(folder/'native-selection-metadata.json').get('personalized_source_extension', {})
            if extension.get('actual_name_manifest_canonical_sha256') and extension.get('source_contract_sha256'):
                covered[extension.get('generation_source_key','native:'+extension['source_contract_sha256'])] = extension['actual_name_manifest_canonical_sha256']

        def compatible(manifest):
            if manifest.get('paced_source'):
                return True
            if covered.get(manifest_key(manifest)) == digest(canonical(manifest)):
                return False
            if manifest.get("quest_variant"):
                return False
            row = current.get(manifest_key(manifest))
            if row is None:
                return False
            installed(addons, row)
            checks = (
                (manifest.get('companion_seed'), row.get('companion_seed')),
                (manifest.get('analysis_base_sha256'), row.get('analysis_base', {}).get('sha256')),
                (manifest.get('reconstructed_caption'), reconstructed_caption(row)),
                (manifest.get('reference_contract'), row.get('reference_contract')),
                (manifest.get('native_source'), native_identity(row)),
            )
            if any(a != b for a, b in checks):
                raise ValueError('An existing Name recipe no longer matches its verified source plan.')
            installed_companion(addons, row, members=members, cache=work/'prepared-companions')
            return True

        result = package_names(packages, archive, include=compatible, trusted_deliveries=state.get('seals'),
                               legacy_playback_paths=legacy_playback_paths, legacy_addons=legacy_addons,
                               preserved_paced_names=state.get('preserved_name_seals'))
        if not result['packaged'] or result['package_holds']:
            raise ValueError('The cumulative coverage update has held or ambiguous routes.')
        return result

    with TemporaryDirectory(dir=work, prefix='.mixed-check-') as temporary:
        prior = Path(temporary)/'prior.zip'
        if expected is not None:
            for legacy_paths in (False, True):
                build(previous, prior, legacy_playback_paths=legacy_paths)
                with ZipFile(prior) as archive:
                    if digest(archive.read(INDEX.as_posix())) == expected:
                        break
            else:
                raise ValueError('The installed companion contains unaccounted routes; coverage was not replaced.')
        else:
            raise ValueError('The installed companion contains unaccounted routes; coverage was not replaced.')
        result = build(incoming, output, legacy_addons=addons)
    return result, expected
