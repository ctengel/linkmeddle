#!/bin/bash
# USAGE: ./addqueue.sh http://1.2.3.4:1234/download/ urlfile.txt
for i in `cat $2`; do echo $i; curl "$1?url=$i"; done
