#!/bin/bash
TASK_DESC="$1"
echo "{\"type\": \"code\", \"description\": \"$TASK_DESC\"}" > /tmp/core_queue.json
echo "Задача передана оркестратору: $TASK_DESC"
