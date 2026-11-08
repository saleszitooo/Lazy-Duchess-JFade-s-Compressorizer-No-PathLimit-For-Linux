This application was ported to Linux from the Windows version posted by Lazy Duchess, which can be found here: https://lazyduchess.tumblr.com/post/684643627850432512/ts2-jfades-compressorizer-no-path-limit

# The Compressorizer — Linux

Compresses (and decompresses) The Sims 2 `.package` files using QFS/RefPack, saving disk space in your Downloads folder.

## Install

Download `The_Compressorizer-x86_64.AppImage` from the **Releases** page.

### Option 1 — Gear Lever (recommended)

[Gear Lever](https://flathub.org/apps/it.mijorus.gearlever) integrates AppImages into your application menu and handles updates and removal.

1. Install Gear Lever (if you don't have it yet):
   ```bash
   flatpak install flathub it.mijorus.gearlever
   ```
2. Open `The_Compressorizer-x86_64.AppImage` with Gear Lever (right-click the file → *Open with Gear Lever*, or drag the file into its window).
3. Click **Unlock** (if prompted), then **Move to the app menu** / **Add to menu**.
4. Done: *The Compressorizer* now shows up in your application menu. To remove it, use Gear Lever itself.

### Option 2 — Run it directly

```bash
chmod +x The_Compressorizer-x86_64.AppImage
./The_Compressorizer-x86_64.AppImage
```

If you get a FUSE error (common on Ubuntu 22.04 and later), install `libfuse2` (`sudo apt install libfuse2`; the package is `libfuse2t64` on Ubuntu 24.04) or run it with `--appimage-extract-and-run`.

## Usage

1. **Back up** your Downloads folder first (files are rewritten in place).
2. Click *Add Files to List* (pick files) or *Add Folder to List* (pick a folder; tick *Scan Subfolders* to include subfolders).
3. Click **GO!** — the *After* column and *Space Saved* fill in as it works. **STOP!** cancels safely.

Extras compared to the Windows version: *Decompress instead* (decompress instead of compress) and *Level* (Fast / Normal / Best).

You can also open it with folders or files already loaded: `./The_Compressorizer-x86_64.AppImage ~/Downloads`.

### Command line

The AppImage also bundles the standalone compressor:

```
./The_Compressorizer-x86_64.AppImage --cli [-d] [-n] [-1|-9] a.package b.package ...
  -d  decompress everything      -n  dry run (does not modify any file)
  -1  fast                       -9  best ratio (slower)
```

## Safety

- Each file is rebuilt in memory and **verified** (every resource is decompressed again and compared byte by byte with the original) before anything is written.
- The new file is written to `file.package.$new` and swapped in with an atomic `rename`; if anything fails, the original stays untouched.
- Invalid or truncated files, and files with duplicate index entries, are skipped and left as they are.
- If the result isn't smaller, the file is not touched.

## Notes

- The DBPF format and QFS compression were reimplemented from the public format description (ModTheSims wiki); this is **not** code from the original `dbpf-recompress.exe`. The compression ratio may differ slightly from the original's.
- The icon is the original Compressorizer icon, by Lazy Duchess.
