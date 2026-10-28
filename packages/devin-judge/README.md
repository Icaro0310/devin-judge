# devin-judge

`devin-judge` is the new PyPI distribution name for the devin-judge tool,
previously published only as `poordjaevin`.

This package is a thin meta-package: it depends on `poordjaevin` and
re-exports its command line interface. Installing it gives you both
commands:

```bash
pip install devin-judge
devin-judge --help
poordjaevin --help   # same CLI, historical name preserved
```

`pip install poordjaevin` keeps working and installs the identical tool.

Source and documentation: https://github.com/Icaro0310/devin-judge
