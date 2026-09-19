# Windows 安装指南

三条路,选一条就行。**推荐路线 A**——不需要装 Python,不需要管理员权限。

---

## 路线 A:下载现成的 .exe(最简单)

### 1. 下载

打开仓库 → **Actions** → 左侧 **Build LocalRedact.exe** → 点最新一次成功的运行 →
页面底部 **Artifacts** → 下载 `LocalRedact-windows-exe`。

得到 `LocalRedact-windows-exe.zip`(约 30 MB),放在"下载"文件夹里就行,**不用解压**。

> 下载 Artifacts 需要登录 GitHub。如果你想要一个不登录就能下的直链,见本文末尾
> "附:发布 Release"。

### 2. 安装

在 `pdf_redactor\packaging\` 目录下**右键 `install.ps1` → 使用 PowerShell 运行**。

或者开一个 PowerShell 窗口(**不需要管理员**):

```powershell
cd <仓库路径>\pdf_redactor
.\packaging\install.ps1
```

脚本会自动在"下载"文件夹里找那个 zip。也可以明确指定:

```powershell
.\packaging\install.ps1 -Source "$env:USERPROFILE\Downloads\LocalRedact-windows-exe.zip"
```

想顺便加上 PDF 右键菜单"Redact with LocalRedact":

```powershell
.\packaging\install.ps1 -ContextMenu
```

**想先看它要做什么、再决定**(强烈推荐第一次这么跑):

```powershell
.\packaging\install.ps1 -DryRun
```

`-DryRun` 只打印每一步,不改动任何东西。

### 3. 启动

开始菜单 → **LocalRedact**,或桌面快捷方式。

---

## 如果 PowerShell 拒绝执行脚本

Windows 默认禁止运行 .ps1。**只对当前这个窗口**放开即可,不影响系统设置:

```powershell
Set-ExecutionPolicy -Scope Process -Bypass
```

然后再跑 `.\packaging\install.ps1`。窗口一关就恢复原样。

---

## 如果出现"Windows 已保护你的电脑"(SmartScreen)

点 **更多信息** → **仍要运行**。

原因:这个 exe 没有代码签名证书。消除这个提示需要购买 EV 代码签名证书(每年数百美元),
对自用工具不值得。安装脚本已经用 `Unblock-File` 清掉了"来自互联网"的标记,
能减少但不能完全消除这个提示。

**不放心的话,这是最稳妥的做法**:不用 exe,走路线 C 从源码运行——你能看到每一行代码。

---

## 如果杀毒软件报毒

PyInstaller 单文件 exe 的常见误报(打包器特征与某些恶意软件相似)。已经做的降低措施:
spec 里关闭了 UPX 压缩,并排除了所有网络库。

可以把 `%LOCALAPPDATA%\Programs\LocalRedact\` 加进白名单,或者走路线 C。

---

## 路线 B:自己构建 .exe

需要 [python.org](https://www.python.org/downloads/windows/) 的 Python 3.9+,
**安装时务必勾选 `tcl/tk and IDLE`**(GUI 需要 Tkinter)。

```bat
cd <仓库路径>\pdf_redactor
packaging\build_exe.bat
```

脚本会建虚拟环境、装依赖、**先跑完整测试套件**(测试不过就拒绝打包)、再调 PyInstaller。
产物在 `dist\LocalRedact.exe`。

然后照路线 A 第 2 步安装(此时 `install.ps1` 会自动找到 `dist\LocalRedact.exe`)。

---

## 路线 C:直接从源码运行(不打包)

最透明的方式,也最适合不想碰 exe 的场景。

```bat
cd <仓库路径>\pdf_redactor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python run_app.py
```

命令行版本:

```bat
python -m app.cli report.pdf -o report_clean.pdf
```

---

## 装扫描件 OCR(可选)

文本型 PDF 不需要这一步。扫描件才需要,两个方案二选一,**都在本机运行**:

**Tesseract**(框更贴合,推荐)
1. 离线安装包:<https://github.com/UB-Mannheim/tesseract/wiki>
2. 中文要勾 `chi_sim` / `chi_tra` 语言包
3. `pip install pytesseract Pillow`

程序按这个顺序找它:`LOCALREDACT_TESSERACT` 环境变量 → exe 同级的 `tesseract\` →
`C:\Program Files\Tesseract-OCR\` → PATH。

**RapidOCR**(纯 pip,模型打包在 wheel 里,首次运行不下载任何东西)
```bat
pip install rapidocr-onnxruntime Pillow
```

装好后重开程序,左下角会显示 `OCR: tesseract (offline)`,"Use local OCR" 才能勾选。

---

## 卸载

```powershell
.\packaging\install.ps1 -Uninstall
```

删除程序目录、开始菜单和桌面快捷方式、右键菜单项。**不会动你的任何 PDF。**

---

## 安装到哪里了

| 项目 | 位置 |
|---|---|
| 程序 | `%LOCALAPPDATA%\Programs\LocalRedact\` |
| 开始菜单 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\LocalRedact.lnk` |
| 桌面快捷方式 | 桌面(支持 OneDrive 重定向) |
| 右键菜单(可选) | `HKCU\Software\Classes\SystemFileAssociations\.pdf\shell\LocalRedact` |

全部在当前用户名下(`HKCU` + `LOCALAPPDATA`),**不需要管理员,不影响本机其他账户,
不写 `Program Files`,不装服务,不设开机自启**。

---

## 一个诚实的说明:关于临时文件

程序本身的临时文件是严格管理的——私有目录、随机字节覆写后删除。

但 **PyInstaller 单文件 exe 有一个我们控制不了的行为**:启动时会把打包的运行时解压到
`%TEMP%\_MEIxxxxxx\`,退出时删除。那里面是**程序自己的代码和依赖库,不含你的 PDF 内容**。
如果程序被强制结束,这个目录可能残留,手动删掉即可。

在意这一点的话走路线 C(从源码运行),就完全没有这个解压步骤。

---

## 常见问题

**`ModuleNotFoundError: tkinter`**(路线 B/C)
用 python.org 官方安装包重装 Python,勾选 `tcl/tk and IDLE`。或用命令行版本。

**双击 exe 后好几秒没反应**
正常。单文件 exe 首次启动要解压约 30 MB,之后会快一些。

**右键菜单点了没反应**
确认安装时加了 `-ContextMenu`。该功能依赖程序接受文件参数(`LocalRedact.exe "文件.pdf"`),
这是支持的。

**提示"不是 PDF 文件"**
程序只处理真正的 PDF。它会解析文件头判断,不是只看扩展名——所以改名成 `.pdf` 的
Word/图片会被拒绝,这是有意为之。

---

## 附:发布 Release(可选,给不想登录 GitHub 的下载方式)

Artifacts 需要登录才能下载。如果你想要一个公开直链,打个 tag 即可:

```bash
git tag localredact-v1.0.0
git push origin localredact-v1.0.0
```

前提是 workflow 里启用了 release job(见 `.github/workflows/build-localredact.yml`
里的说明)。**我没有替你打这个 tag,也没有建 Release**——那是对外发布动作,由你决定。
