#!/bin/bash
celery -A tasks worker --loglevel=INFO -c 1
