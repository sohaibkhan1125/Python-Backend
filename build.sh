#!/usr/bin/env bash
set -e

# Install ffmpeg on Debian/Ubuntu-based build image (if allowed)
if [ -x "$(command -v apt-get)" ]; then
  sudo apt-get update -y
  sudo apt-get install -y ffmpeg
fi

# Install Python deps
python -m pip install --upgrade pip
pip install -r requirements.txt
