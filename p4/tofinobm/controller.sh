#!/bin/bash

if [ ! -d $SDE_INSTALL ]; then
    echo "BF SDE is not defined"
    exit 1
fi

export PYTHONPATH=$SDE_INSTALL/lib/python2.7/site-packages/tofinobm/pdfixed:$SDE_INSTALL/lib/python2.7/site-packages/tofinobmpd/:$PYTHONPATH

python $(dirname $0)/controller.py --config $1
