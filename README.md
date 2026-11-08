# vslocator

A dependency-free, single-file Python reimplementation of Microsoft's
[`vswhere`](https://github.com/microsoft/vswhere), for locating Visual Studio
installations on Windows.

**Output compatibility:** on Windows 11 with Visual Studio 2022 Community 17.14
and Visual Studio 2026 Community 18.11, `vslocator` was compared against
`vswhere 3.1.7`. JSON output is field-for-field identical including key order,
text and XML output is byte-for-byte identical, and `-include packages` returns
the same package set. See
[Output compatibility](#output-compatibility-with-vswhere).

[中文说明](README.zh-CN.md)

## How it works

`vswhere.exe` uses the Visual Studio Setup Configuration COM API. That API is
backed by the state the VS installer writes under `%ProgramData%`:

```
%ProgramData%\Microsoft\VisualStudio\Packages\_Instances\<instanceId>\
    state.json            identity, paths, catalog, localized names, ...
    state.packages.json   every package installed in the instance
```

`vslocator` reads those two small JSON files directly. It does not call
`vswhere.exe`, does not need COM registration, and has no third-party
dependencies. The 18 MB `catalog.json` next to them is never read.

## Install

```bat
python -m pip install vslocator
```

or, for an isolated command-line install:

```bat
pipx install vslocator
```

From a source checkout:

```bat
python -m pip install .
```

## Usage

```bat
vslocator -latest -property installationPath
vslocator -all -products * -format json -utf8
vslocator -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
vslocator -version "[17.0,18.0)" -property installationVersion
vslocator -path C:\src\myproject -property installationPath
vslocator -find "Common7\IDE\devenv.exe"
```

From a source checkout without installing:

```bat
python vslocator.py -latest -property installationPath
```

Supported options:

```
-all  -prerelease  -products  -requires  -requiresAny  -version
-latest  -sort  -path  -format {json,text,value,xml}  -property
-include packages  -find  -nologo  -nocolor  -utf8  -?, -h, -help
```

Exit codes: `0` success, `2` command-line syntax error, `87` invalid option
value (the same `ERROR_INVALID_PARAMETER` value `vswhere` uses).

## Output compatibility with vswhere

Tested on Windows 11 with:

- Visual Studio 2022 Community 17.14.37827.9
- Visual Studio 2026 Community 18.11.12328.437
- `vswhere 3.1.7+f39851e70f [query version 4.8.41.48107]`

| Output | Result |
| --- | --- |
| `-format json` | field-for-field identical, including key order |
| `-format text` (`-nologo`) | byte-for-byte identical |
| `-format xml` | byte-for-byte identical |
| `-property`, `-find`, `-version`, `-requires`, `-path` | identical |
| `-include packages` | identical package set; array order may differ |

The package array order comes from the Setup API in `vswhere` and from
`state.packages.json` here. Compare packages as a set when order matters.

## Relationship to mjansson/vslocate

The minimal [`mjansson/vslocate`](https://github.com/mjansson/vslocate) project
(public domain, about 160 lines) does one thing: it loads the Setup
Configuration DLL directly and returns `(version, path)` pairs. `vslocator` is
a full vswhere-compatible CLI with filters, packages and JSON/XML output. They
are independent implementations; if you only need a version and a path, the
minimal project may be a better fit.

## Tests

```bat
python -m unittest discover -s tests -v
```

The suite has two layers:

- synthetic instances under a temporary directory, covering parsing, filtering,
  ordering, formatting and the CLI;
- real-machine tests that compare `vslocator` with `vswhere` field-by-field and
  byte-by-byte. They skip automatically when `vswhere.exe` or Visual Studio is
  not installed.

## Limitations

- Visual Studio 2017 and newer only; `-legacy` (VS 2015 and older) is not
  supported.
- `isComplete`, `isLaunchable` and `isRebootRequired` are derived from the
  instance state. They match the Setup API for normal, complete installations,
  but are approximate for broken or half-installed instances.
- Windows only.
- `vswhere` has a quirk where a version range with a `[0.0,...` lower bound
  ignores the upper bound. `vslocator` follows standard NuGet range semantics
  instead of copying that bug.
- No logo is printed; `-nologo` is accepted for compatibility.

## License

MIT. See [LICENSE](LICENSE).
