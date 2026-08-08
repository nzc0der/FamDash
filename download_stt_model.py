#!/usr/bin/env python3
"""
Lightweight offline model downloader for Speech-To-Text.
Downloads highly optimized GGML models for Whisper (tiny.en by default) for use in offline voice processing pipelines.
"""

import os
import sys
import urllib.request

# Configuration
MODEL_NAME = "tiny.en"
MODELS_DIR = "data/models"
MODEL_FILENAME = f"ggml-{MODEL_NAME}.bin"
DOWNLOAD_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_FILENAME}"

def main():
    print("=== Family Dashboard Speech-To-Text Model Downloader ===")

    # Ensure models directory exists
    os.makedirs(MODELS_DIR, exist_ok=True)
    target_path = os.path.join(MODELS_DIR, MODEL_FILENAME)

    if os.path.exists(target_path):
        print(f"[*] Model '{MODEL_NAME}' is already downloaded at: {target_path}")
        print("[*] Ready for offline/local transcription.")
        return 0

    print(f"[*] Target directory: {MODELS_DIR}")
    print(f"[*] Target path: {target_path}")
    print(f"[*] Downloading '{MODEL_NAME}' Whisper model (highly optimized, fast, and accurate)...")
    print(f"[*] Source: {DOWNLOAD_URL}")

    try:
        def progress_callback(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if total_size > 0:
                percent = min(100, int(downloaded * 100 / total_size))
                # Print progress bar
                bar_len = 40
                filled_len = int(bar_len * percent / 100)
                bar = '=' * filled_len + '-' * (bar_len - filled_len)
                sys.stdout.write(f"\r[{bar}] {percent}% ({downloaded / (1024*1024):.2f}MB / {total_size / (1024*1024):.2f}MB)")
                sys.stdout.flush()

        urllib.request.urlretrieve(DOWNLOAD_URL, target_path, reporthook=progress_callback)
        sys.stdout.write("\n")
        print("[+] Download complete successfully!")
        print(f"[+] Model saved to: {target_path}")
        return 0
    except Exception as e:
        sys.stdout.write("\n")
        print(f"[-] Error downloading model: {e}", file=sys.stderr)
        print("[-] Please ensure you have internet connectivity and retry.", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
