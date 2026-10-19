# Third-party code, data and credits

## Code

**tinylight/drivers/nanlite_mesh/** (`mesh_proxy.py`, `crypto.py`, `provision.py`, `config.py`, `protocol.py`)
is derived from [nicholasmullikin/nanlite](https://github.com/nicholasmullikin/nanlite) (Nanlite FS-300B
Bluetooth mesh tooling), MIT License. Changes: macOS device matching, write-without-response for the proxy,
shorter segment gaps, signed G/M in `set_mode` CCT, HSI `set_mode`, package-relative imports. Original notice:

```
MIT License

Copyright (c) 2025 Nicholas Mullikin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Protocol knowledge

- [vmedea's Nanlite protocol notes](https://gist.github.com/vmedea/434694c11092261fcac401b7a4b9a741):
  parameter numbering and the extended `set_mode` packet (CCT 02:02, HSI 03:01).
- [yuyx0407/nanlite-wand-bridge](https://github.com/yuyx0407/nanlite-wand-bridge): published the Feasycom TEA
  authentication key from the NANLINK app (`libencrypted.so`). The key in `drivers/nanlite_mesh/config.py`
  is the same value; you can override it with `FEASYCOM_TEA_KEY` or `$TINYLIGHT_HOME/tea_key.hex`.
- Nanlite and NANLINK are trademarks of their owner. tinyLight is not affiliated with Nanlite.

## Sounds (all CC0, from freesound.org; loudness-normalised to -20 LUFS, AAC)

| File | Source | Author |
|---|---|---|
| sounds/birds-spain.m4a | [Morning Birdsong, Spain](https://freesound.org/s/361411/) | richwise |
| sounds/dawn-chorus-hampshire.m4a | [Dawn chorus - Hampshire countryside](https://freesound.org/s/466242/) | richwise |
| sounds/dawn-lakeside-germany.m4a | [Dawn Chorus at a Lakeside Marina](https://freesound.org/s/852696/) | Garuda1982 |
| sounds/mountain-stream.m4a | [Mountains, Rivers, Streams, Running Water](https://freesound.org/s/469009/) | INNORECORDS |
| sounds/wind-chimes.m4a | [Wind Chimes 02](https://freesound.org/s/222893/) | Adelantemos |

## Libraries (installed, not vendored)

bleak (MIT), cryptography (Apache-2.0 / BSD), HAP-python (Apache-2.0, optional).
