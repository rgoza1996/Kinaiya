# Third-party notices

meta-sam is licensed under the [SAM License](LICENSE). The components below are
included in this repository, or in the packages built from it, under their own
licenses. Each notice is reproduced here as those licenses require.

## COCO API mask module

`typescript/packages/parser/src/coco-rle.ts` and
`python/src/meta_sam_parser/_mask_conversion.py` contain ports of the run-length
encoding routines from the COCO API (`common/maskApi.c` and
`PythonAPI/pycocotools/_mask.pyx`, https://github.com/cocodataset/cocoapi). Those
files ship inside `@meta-sam/parser` and `meta-sam-parser`.

```text
Copyright (c) 2014, Piotr Dollar and Tsung-Yi Lin
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The views and conclusions contained in the software and documentation are those
of the authors and should not be interpreted as representing official policies,
either expressed or implied, of the FreeBSD Project.
```

## Playground example media

`typescript/examples/api-playground/public/media/bedroom.mp4`, `truck.jpg`, and
`groceries.jpg` are copied unchanged from the `assets/` directory of
https://github.com/facebookresearch/sam3, which distributes them under the same
[SAM License](LICENSE) as this repository. They are served only by the example
playground and are not part of any published package.

## changesets/action test fixtures

`typescript/scripts/fixtures/changesets-action-a45c4d594aa4e2c509dc14a9f2b3b67ba3780d0d*`
are verbatim copies of files from https://github.com/changesets/action at commit
`a45c4d594aa4e2c509dc14a9f2b3b67ba3780d0d`. They pin the behavior the release
workflow depends on and are used only by repository tests; they are not part of
any published package.

```text
MIT License

Copyright (c) Changesets team and other contributors

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
