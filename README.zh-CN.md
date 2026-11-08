# vslocator

一个零依赖、单文件的 Python 版 [`vswhere`](https://github.com/microsoft/vswhere)，
用于定位 Windows 上安装的 Visual Studio 实例。

**输出一致性：** 在 Windows 11、Visual Studio 2022 Community 17.14 和
Visual Studio 2026 Community 18.11 上，与 `vswhere 3.1.7` 逐项对比：JSON 的字段和
键顺序完全一致，text 和 XML 逐字节一致，`-include packages` 的包集合完全一致。
详见[与 vswhere 的输出一致性](#与-vswhere-的输出一致性)。

[English](README.md)

## 原理

`vswhere.exe` 调用的是 Visual Studio Setup Configuration COM API。该 API 的数据来自
VS 安装器写在 `%ProgramData%` 下的实例状态：

```
%ProgramData%\Microsoft\VisualStudio\Packages\_Instances\<instanceId>\
    state.json            身份、路径、catalog、本地化名称、属性……
    state.packages.json   该实例安装的全部包
```

`vslocator` 直接读取这两个小 JSON 文件：不调用 `vswhere.exe`，不需要 COM 注册，
也没有第三方依赖。旁边的 18 MB `catalog.json` 完全不会读取。

## 安装

```bat
python -m pip install .
```

或者用 pipx 安装成独立的命令行工具：

```bat
pipx install .
```

目前尚未发布到 PyPI。

## 用法

```bat
vslocator -latest -property installationPath
vslocator -all -products * -format json -utf8
vslocator -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
vslocator -version "[17.0,18.0)" -property installationVersion
vslocator -path C:\src\myproject -property installationPath
vslocator -find "Common7\IDE\devenv.exe"
```

不安装、直接从源码运行：

```bat
python vslocator.py -latest -property installationPath
```

支持的选项：

```
-all  -prerelease  -products  -requires  -requiresAny  -version
-latest  -sort  -path  -format {json,text,value,xml}  -property
-include packages  -find  -nologo  -nocolor  -utf8  -?, -h, -help
```

退出码：成功 `0`，命令行语法错误 `2`，非法选项值 `87`（与 `vswhere` 使用的
`ERROR_INVALID_PARAMETER` 相同）。

## 与 vswhere 的输出一致性

测试环境：

- Windows 11
- Visual Studio 2022 Community 17.14.37827.9
- Visual Studio 2026 Community 18.11.12328.437
- `vswhere 3.1.7+f39851e70f [query version 4.8.41.48107]`

| 输出 | 结果 |
| --- | --- |
| `-format json` | 字段和键顺序完全一致 |
| `-format text`（`-nologo`） | 逐字节一致 |
| `-format xml` | 逐字节一致 |
| `-property`、`-find`、`-version`、`-requires`、`-path` | 输出一致 |
| `-include packages` | 包集合完全一致；数组顺序可能不同 |

`vswhere` 的包顺序来自 Setup API 枚举顺序，本工具的包顺序来自
`state.packages.json`。需要比较顺序时请按集合处理。

## 与 mjansson/vslocate 的关系

极简实现 [`mjansson/vslocate`](https://github.com/mjansson/vslocate)（public domain，
约 160 行）只做一件事：直接加载 Setup Configuration DLL，返回 `(version, path)`。
`vslocator` 是功能完整的 vswhere 兼容 CLI，支持过滤、包信息和 JSON/XML 输出。
两者是独立实现；如果你只需要版本号和路径，极简版可能更合适。

## 测试

```bat
python -m unittest discover -s tests -v
```

测试分两层：

- 在临时目录中构造合成实例，覆盖解析、过滤、排序、格式化和 CLI；
- 真机测试，将 `vslocator` 与 `vswhere` 逐字段、逐字节比较；未安装
  `vswhere.exe` 或 Visual Studio 时自动跳过。

## 限制

- 只支持 Visual Studio 2017 及更新版本；不支持 `-legacy`（VS 2015 及更早）。
- `isComplete`、`isLaunchable`、`isRebootRequired` 是从实例状态推导的。正常完整的
  安装与 Setup API 一致，异常或半安装实例上是近似判断。
- 仅支持 Windows。
- `vswhere` 在版本区间下界写成 `[0.0,...` 时会忽略上界（疑似其 parser 的特殊行为）。
  本工具按标准 NuGet 区间语义处理，不复制这个 bug。
- 默认不打印 logo；`-nologo` 仅为兼容而接受。

## License

MIT，见 [LICENSE](LICENSE)。
