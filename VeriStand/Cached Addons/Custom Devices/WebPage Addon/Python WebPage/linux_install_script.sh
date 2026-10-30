#!/bin/bash

echo "Installing Python..." 
opkg install python3-pip
echo "Moving to User Directory..."
cd '/home/lvuser/natinst'
echo "Creating dashboard Directory..."
mkdir 'dashboard'
echo "Marking dashboard writeable..."
chmod a+rwx 'dashboard'
echo "Moving to dashboard Directory..."
cd 'dashboard'
echo "Creating Virtual Environment..."
python -m venv venv
echo "Activate Virtual Environment..."
source venv/bin/activate
echo "Upgrading PiP..."
python -m pip install --upgrade pip
echo "Install gRPC tools..."
pip install grpcio-tools

