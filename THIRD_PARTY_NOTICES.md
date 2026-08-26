# 第三方 / Third-party notices

Ombre Brain 本体是 MIT（见 [LICENSE](LICENSE)，v2.4.1 另有 [非商业声明](LICENSE.v2.4.0-NONCOMMERCIAL-NOTICE.md)）。
下面这些是从别处搬进来的东西，各自的授权也一并列在这。

---

## KaomojiDrawerKit

- 来源：<https://github.com/Pyruslili/KaomojiDrawerKit>
- 授权：MIT License, Copyright (c) 2026 Pyruslili
- 用在哪：Dashboard「信件 → 写一封」正文框旁边的颜文字抽屉
  （`dashboard.html` / `frontend/dashboard.html` 里的 `#kaomoji-drawer`）
- 搬了什么：21 个分类共 204 个自带颜文字（`DefaultKaomoji.json`）、抽屉本身的
  交互（横向分类 tab、点一下只插入不发送、编辑态增删、本地持久化）、以及原仓库
  自带的三个矢量图标（face / close / tidy）
- 改了什么：原版是 iOS / macOS 的 SwiftUI 组件，这边没有 Swift 侧，所以整体重写
  成纯 JS + HTML；存储从 `UserDefaults` 换成 `localStorage`（键名沿用原版的
  `KaomojiDrawerKit.library.v1`，数据结构不变）；配色换成仪表板这套暖米灰；
  图标里的 `#000` 换成 `currentColor`

```
MIT License

Copyright (c) 2026 Pyruslili

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
