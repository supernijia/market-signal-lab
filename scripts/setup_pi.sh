#!/bin/bash

echo "Starting Market Signal Lab Setup for Raspberry Pi..."

# 1. Update System
sudo apt-get update

# 2. Install System Dependencies (for Pandas/Numpy speed on Pi)
echo "Installing system libraries..."
sudo apt-get install -y python3-pip python3-dev libatlas-base-dev gfortran

# 3. Install Python Dependencies
echo "Installing Python requirements..."
# Try to install with pi user scope or break-system-packages (Debian 12+)
pip3 install -r requirements.txt --break-system-packages || pip3 install -r requirements.txt

echo "Setup Complete! You can now run the analyzer."
