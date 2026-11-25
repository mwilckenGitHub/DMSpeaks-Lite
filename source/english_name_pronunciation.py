"""English pronunciation additions for names absent from the local CMU dictionary."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from ctypes import CDLL, POINTER, addressof, byref, c_char_p, c_int, c_void_p, create_string_buffer
from ctypes.util import find_library

# eSpeak's American-English IPA output uses one unit per separator, including
# combined rhotic vowels and diphthongs. Refuse unmapped units.
IPA_TO_CMU = {
    'p': 'P', 'b': 'B', 't': 'T', 'd': 'D', 'ɾ': 'T', 'k': 'K', 'ɡ': 'G',
    'f': 'F', 'v': 'V', 'θ': 'TH', 'ð': 'DH', 's': 'S', 'z': 'Z',
    'ʃ': 'SH', 'ʒ': 'ZH', 'tʃ': 'CH', 'dʒ': 'JH', 'h': 'HH', 'ɹ': 'R',
    'r': 'R', 'l': 'L', 'm': 'M', 'n': 'N', 'ŋ': 'NG', 'w': 'W', 'j': 'Y',
    'i': 'IY', 'ɪ': 'IH', 'ɛ': 'EH', 'e': 'EH', 'æ': 'AE', 'ɑ': 'AA',
    'a': 'AA', 'ɒ': 'AA', 'ɔ': 'AO', 'ʊ': 'UH', 'u': 'UW', 'ʌ': 'AH',
    'ə': 'AH', 'ɐ': 'AH', 'ɜ': 'ER', 'ɚ': 'ER', 'ɝ': 'ER', 'aɪ': 'AY', 'aʊ': 'AW',
    'ɔɪ': 'OY', 'eɪ': 'EY', 'oʊ': 'OW', 'o': 'OW', 'əʊ': 'OW',
    'ɑɹ': 'AA R', 'ɔɹ': 'AO R', 'oɹ': 'AO R', 'ɛɹ': 'EH R',
    'eɹ': 'EH R', 'ɪɹ': 'IH R', 'ʊɹ': 'UH R', 'aɪə': 'AY AH',
    'aɪɚ': 'AY ER', 'iə': 'IY AH',
    'əl': 'AH L', 'ən': 'AH N', 'əm': 'AH M', 'ᵻ': 'IH',
}
STRESS = str.maketrans('', '', 'ˈˌːˑ͡')


def pronunciation_runtime():
    """Prefer a complete companion runtime; keep explicit user overrides."""
    root=Path(__file__).resolve().parent.parent/'runtime/espeak'
    library=os.environ.get('DMSPEAKS_ESPEAK_LIBRARY')
    data=os.environ.get('DMSPEAKS_ESPEAK_DATA')
    bundled=False
    if not library and root.exists():
        filename=('libespeak-ng.dll' if os.name=='nt' else
                  'libespeak-ng.dylib' if sys.platform=='darwin' else 'libespeak-ng.so')
        library=str(root/filename);data=data or str(root);bundled=True
        if not Path(library).is_file():
            raise ValueError('The bundled pronunciation library is missing. Extract the complete companion runtime.')
    if data:
        parent=Path(data).expanduser().resolve()
        folder=parent if parent.name=='espeak-ng-data' else parent/'espeak-ng-data'
        required=['phondata','phonindex','phontab','intonations','en_dict','lang/gmw/en-US']
        if not all((folder/name).is_file() for name in required):
            raise ValueError('The pronunciation runtime needs its complete English voice data.')
        data=str(folder)
    library=library or find_library('espeak-ng')
    if not library:
        raise ValueError('The English pronunciation runtime is unavailable.')
    return library,data,bundled


def cmu_phones(units):
    """Map American-English eSpeak units to this acoustic model's inventory."""
    phones = []
    for index, unit in enumerate(units):
        following = units[index + 1] if index + 1 < len(units) else None
        if unit == 'ʔ' and following in ('n', 'n̩'):
            # eSpeak realizes /t/ as a glottal stop before the nasal.
            phones.append('T')
        elif unit == 'n̩':
            phones.append('AH N')
        elif unit in IPA_TO_CMU:
            phones.append(IPA_TO_CMU[unit])
        else:
            return None
    return ' '.join(phones) or None


class _Engine:
    def __init__(self, library, data, bundled):
        self.dll_directory=(os.add_dll_directory(str(Path(library).parent))
                            if bundled and os.name=='nt' else None)
        self.engine = CDLL(library)
        self.engine.espeak_Initialize.argtypes = [c_int, c_int, c_char_p, c_int]
        self.engine.espeak_SetVoiceByName.argtypes = [c_char_p]
        self.engine.espeak_TextToPhonemes.argtypes = [POINTER(c_void_p), c_int, c_int]
        self.engine.espeak_TextToPhonemes.restype = c_char_p
        if self.engine.espeak_Initialize(2, 0, data.encode('utf-8') if data else None, 0x8000) <= 0:
            raise RuntimeError('eSpeak NG could not initialize.')
        if self.engine.espeak_SetVoiceByName(b'en-us') != 0:
            raise RuntimeError('The eSpeak NG American English voice is unavailable.')

    def pronunciation(self, word):
        buffer = create_string_buffer(word.encode())
        address = c_void_p(addressof(buffer))
        ipa = self.engine.espeak_TextToPhonemes(byref(address), 1, (ord('|') << 8) | 2)
        if not ipa:
            raise ValueError('The English pronunciation could not be determined.')
        units = [unit.translate(STRESS) for unit in re.split(r'[|\s]+', ipa.decode()) if unit]
        return cmu_phones(units)


class Pronouncer:
    def __init__(self):
        library, data, bundled = pronunciation_runtime()
        # The packaged DLL opens data files through narrow Windows CRT calls.
        # A worker can read them relative to a Unicode working directory.
        self.relative_data = data if os.name == 'nt' and data and not data.isascii() else None
        self.engine = None if self.relative_data else _Engine(library, data, bundled)
        if self.relative_data:
            # Preserve a caller-supplied DLL path when the worker changes cwd.
            self.library = str(Path(library).absolute()) if Path(library).is_file() else library
            self.bundled = bundled

    def pronunciation(self, word):
        if self.engine is not None:
            return self.engine.pronunciation(word)
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        try:
            result = subprocess.run(
                [str(python), '-I', '-B', '-X', 'utf8', __file__, '--relative-data',
                 self.library, 'bundled' if self.bundled else 'external'],
                cwd=self.relative_data, input=json.dumps(word), capture_output=True,
                encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=True,
            )
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ValueError('eSpeak NG could not read its English voice data from this Windows path.') from error


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--relative-data':
        library, mode = sys.argv[2:]
        print(json.dumps(_Engine(library, '.', mode == 'bundled').pronunciation(json.load(sys.stdin))))


if __name__ == '__main__':
    main()

