#!/bin/bash
# Watch LiDAR bake containers: emit events on container exit, new COG, low disk.
# Heartbeat every 10 min with compact state. Poll 60s.
OUT=/d/ps_lidar/data/global/lidar
WORK=$OUT/_work
prev_running=""
prev_cogs=""
warned_disk=0
tick=0
while true; do
  running=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^bake_|angry_lewin' | sort | tr '\n' ' ')
  cogs=$(ls "$OUT"/*_5m.tif 2>/dev/null | xargs -n1 basename 2>/dev/null | sort | tr '\n' ' ')
  freeg=$(df -m /d 2>/dev/null | awk 'NR==2{printf "%d", $4/1024}')

  # container set changed -> a bake finished or died
  if [ "$running" != "$prev_running" ] && [ -n "$prev_running" ]; then
    for c in $prev_running; do
      case " $running " in *" $c "*) ;; *)
        echo "EXITED: $c | now-running: ${running:-none} | free ${freeg}G"
        zone=${c#bake_}; [ "$c" = "angry_lewin" ] && zone=waz51
        tail -3 "/d/ps_lidar/bake_${zone}.log" 2>/dev/null | sed 's/^/  /'
      ;; esac
    done
  fi
  # new COGs appeared
  if [ "$cogs" != "$prev_cogs" ]; then
    echo "COGS NOW: ${cogs:-none} | free ${freeg}G"
  fi
  # disk guard
  if [ -n "$freeg" ] && [ "$freeg" -lt 80 ] && [ "$warned_disk" = 0 ]; then
    echo "DISK LOW: ${freeg}G free on D:"
    warned_disk=1
  fi
  # heartbeat every 10 polls
  tick=$((tick+1))
  if [ $((tick % 10)) -eq 0 ]; then
    sizes=$(ls -l "$WORK" 2>/dev/null | awk '$5>1e6{printf "%s=%.1fG ", $NF, $5/1e9}')
    echo "HEARTBEAT: running=[${running:-none}] free=${freeg}G work:[${sizes:-empty}]"
  fi
  prev_running="$running"
  prev_cogs="$cogs"
  sleep 60
done
