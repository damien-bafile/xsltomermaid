set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

py := "3.14"
uv := "uv --system-certs"

default:
    @just --list

# Install dependencies (incl. dev tools) into .venv
sync:
    {{uv}} sync --python {{py}}

# Launch the GUI, optionally opening a file
run *args:
    {{uv}} run --python {{py}} xsltomermaid {{args}}

# Write sample_schema.xlsx
sample:
    {{uv}} run --python {{py}} xsltomermaid-sample

# Run the test suite
test *args:
    $env:QT_QPA_PLATFORM = "offscreen"; {{uv}} run --python {{py}} pytest {{args}}

# Build the standalone executable with PyInstaller
build:
    {{uv}} run --python {{py}} pyinstaller xsltomermaid.spec

# Remove build artifacts and caches
clean:
    Remove-Item -Recurse -Force -ErrorAction Ignore build, dist, .pytest_cache; exit 0
    Get-ChildItem src, tests -Recurse -Directory -Filter __pycache__ -ErrorAction Ignore | ForEach-Object { Remove-Item -Recurse -Force $_.FullName }; exit 0

# Remove everything from clean plus the virtualenv
clean-all: clean
    Remove-Item -Recurse -Force -ErrorAction Ignore .venv; exit 0

# Recreate the virtualenv from scratch
reset: clean-all sync
