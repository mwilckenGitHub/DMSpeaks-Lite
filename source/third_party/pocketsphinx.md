# PocketSphinx phoneme alignment

DMSpeaks uses an optional local executable built from PocketSphinx 5.1.1. The app adapter is [pocketsphinx_phonemes.py](../pocketsphinx_phonemes.py). It runs PocketSphinx with an installed acoustic model and pronunciation dictionary, reads its word and phone intervals, and records them in the sample-clock format in [phoneme_alignment.py](../phoneme_alignment.py). DMSpeaks' snapping decisions are in [phoneme_snap.py](../phoneme_snap.py).

| Item | Provenance |
| --- | --- |
| Source repository | [cmusphinx/pocketsphinx](https://github.com/cmusphinx/pocketsphinx) |
| Release | [PocketSphinx 5.1.1](https://pypi.org/project/pocketsphinx/5.1.1/#files) |
| Release source SHA-256 | `675778b309a22dfc9b7d37f7621976bba491d2a5f8c59696bd77fd6d07271355` |
| License and dictionary notices | `LICENSE` in the release archive |
| Acoustic model notice | `model/en-us/en-us/README` in the release archive |
| US English dictionary SHA-256 | `20b5c293e1f311fb375fe067e500ec5636f4fc7af5594967263696def9b23bfe` |

The Spark development copy was compiled with CMake as a native Linux ARM64 executable. Set the source and build paths for an extracted, checksum-verified release.

```sh
cmake -S "$POCKETSPHINX_SOURCE" -B "$POCKETSPHINX_BUILD" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
cmake --build "$POCKETSPHINX_BUILD" --target pocketsphinx_main
```

The HTML beta recognizes an optional default `[phoneme_alignment]` section in `providers.ini`. Set `executable`, `acoustic_model`, and `dictionary` to local paths. Set `profile_id` to the exact model version, `language` to its locale, and `phone_set` to its notation. Existing defaults without the last two fields use `en-US` and ARPAbet.

For another locale, use a section such as `[phoneme_alignment:fr-FR]` with a matching acoustic model and dictionary. The section name supplies the language and `phone_set` is required. DMSpeaks tries the dialogue locale, a configured parent locale, and then a compatible default. The adapter accepts Unicode letters and accents in word-separated transcripts. Languages needing a different tokenizer can supply measured word and phone intervals through the alignment importer.

On the shared Spark, set `inference_lease` in each installed profile to the shared runtime lease path. The aligner runs on the CPU when requested and caches evidence by audio and transcript digest. Cached evidence must also match the dialogue language.

For the C++ port, the upstream C library exposes the same two passes used by the executable. Use `ps_set_align_text` for the first pass and `ps_set_alignment` for the second. Read the word and phone intervals through the alignment API and verify that the resulting sample-clock coordinates match the adapter's results. The upstream release remains identifiable as a separately pinned dependency.

When distributing a build with PocketSphinx or its packaged data, include the full release notices and the acoustic-model notice in Third-Party Notices. The HTML beta source adapter can also import external Montreal Forced Aligner output with [mfa_phonemes.py](../mfa_phonemes.py). Each separately distributed language model and dictionary has its own license.
