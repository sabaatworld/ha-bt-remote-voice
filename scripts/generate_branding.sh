#!/usr/bin/env bash
# Render the SVG sources into shared Home Assistant and README PNG assets.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_dir/docs/images"
brand_dir="$repo_dir/custom_components/atvv_remote/brand"
assets=(icon logo dark_logo)

if ! command -v rsvg-convert >/dev/null 2>&1; then
  echo "Missing rsvg-convert. Install librsvg, then run this script again." >&2
  exit 1
fi
for asset in "${assets[@]}"; do
  if [[ ! -f "$source_dir/$asset.svg" ]]; then
    echo "Missing SVG source: $source_dir/$asset.svg" >&2
    exit 1
  fi
done

staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/atvv-branding.XXXXXX")"
trap 'rm -rf -- "$staging_dir"' EXIT
# Finish every render before replacing any existing output.
for asset in "${assets[@]}"; do
  for scale in 1 2 3; do
    suffix=""
    if [[ "$scale" != 1 ]]; then
      suffix="@${scale}x"
    fi
    rsvg-convert --height "$((256 * scale))" \
      --output "$staging_dir/$asset$suffix.png" "$source_dir/$asset.svg"
  done
done
mkdir -p -- "$brand_dir"
for output in "$staging_dir"/*.png; do
  mv -- "$output" "$brand_dir/"
done
printf 'Generated nine PNGs (1x, 2x, 3x) in %s\n' "$brand_dir"
