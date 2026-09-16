#!/bin/bash
set -euo pipefail

docker_clean() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/var/empty DOCKER_CONFIG=/var/empty DOCKER_BUILDKIT=0 \
        /Applications/Docker.app/Contents/Resources/bin/docker --config /var/empty \
        --host unix:///var/run/docker.sock "$@"
}
for path in /var/empty/config.json /var/empty/contexts /var/empty/cli-plugins; do
    [ ! -e "$path" ] && [ ! -L "$path" ]
done
MODE=${1:-all}
case "$MODE" in all|worker|identity) ;; *) exit 2 ;; esac
TASK=axe-step03c-$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')
OUT=validation/step03c/evidence-$TASK
/bin/mkdir "$OUT"
printf '%s\n' "$TASK" > "$OUT/task.txt"
# Only Python source/tests and explicitly named validation/configuration inputs enter the context.
[ -z "$(/usr/bin/find src tests scripts validation/step03c -type l -print)" ]
/usr/bin/find src tests scripts -type f -name '*.py' -print > "$OUT/context-files.txt"
printf '%s\n' pyproject.toml scripts/check_isolation_policy.py \
    validation/step03c/Dockerfile validation/step03c/runner.py validation/step03c/test_settings.py \
    validation/step03b/installed-18fb41f9-526b-4423-a7b1-c64b99759f0c/dependencies >> "$OUT/context-files.txt"
/usr/bin/tar --format=gnutar -cf "$OUT/context.tar" -T "$OUT/context-files.txt"
docker_clean build --no-cache --platform linux/arm64 --pull=false --network none --rm=false --force-rm=false \
    -f validation/step03c/Dockerfile -t "$TASK:local" - < "$OUT/context.tar" > "$OUT/build.log" 2>&1
IMAGE=$(docker_clean image inspect "$TASK:local" --format '{{.Id}}')
ROOT_OUT=$OUT
ROOT_TASK=$TASK
GROUPS_TO_RUN='baseline types policy'
[ "$MODE" = all ] || GROUPS_TO_RUN=$MODE
FAILED=0
for GROUP in $GROUPS_TO_RUN; do
TASK=$ROOT_TASK-$GROUP
OUT=$ROOT_OUT/$GROUP
/bin/mkdir "$OUT"
MEMORY=512m
BYTES=536870912
if [ "$GROUP" = types ]; then
    MEMORY=2g
    BYTES=2147483648
fi
docker_clean volume create "$TASK-scratch" > "$OUT/volume.txt"
CID=$(docker_clean create --name "$TASK" --pull never --platform linux/arm64 --network none \
    --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --ipc none \
    --cpus 1 --memory "$MEMORY" --memory-swap "$MEMORY" --pids-limit 64 --log-driver local \
    --log-opt max-size=1m --log-opt max-file=1 --log-opt compress=false \
    --mount "type=volume,source=$TASK-scratch,target=/scratch" "$IMAGE" --group "$GROUP")
docker_clean inspect "$CID" > "$OUT/inspect-before.json"
actual=$(docker_clean inspect "$CID" --format '{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidMode}}|{{.HostConfig.IpcMode}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{len .Mounts}}|{{len .HostConfig.Devices}}|{{len .HostConfig.PortBindings}}|{{with index .HostConfig "Tmpfs"}}{{len .}}{{else}}0{{end}}')
[ "$actual" = "true|10001:10001|none|false||none|1000000000|$BYTES|$BYTES|64|[\"ALL\"]|[\"no-new-privileges:true\"]|1|0|0|0" ]
[ "$(docker_clean inspect "$CID" --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}|{{.RW}}{{end}}')" = "volume|$TASK-scratch|/scratch|true" ]
[ "$(docker_clean inspect "$CID" --format '{{.Image}}')" = "$IMAGE" ]
docker_clean start "$CID"
SECONDS=0
TIMED_OUT=false
while [ "$(docker_clean inspect "$CID" --format '{{.State.Status}}')" = running ]; do
    if [ "$SECONDS" -ge 600 ]; then
        TIMED_OUT=true
        docker_clean stop --time 2 "$CID"
        break
    fi
    /bin/sleep 1
done
docker_clean logs "$CID" > "$OUT/run.log" 2>&1
docker_clean inspect "$CID" > "$OUT/inspect-after.json"
/bin/cat "$OUT/run.log"
printf 'evidence=%s\nimage=%s\n' "$OUT" "$IMAGE"
# Reports are task-owned synthetic outputs, copied even when a check fails.
docker_clean cp "$CID:/scratch/reports" "$OUT/reports" || printf 'reports_unavailable\n'
if [ "$TIMED_OUT" != false ] || [ "$(docker_clean inspect "$CID" --format '{{.State.ExitCode}}|{{.State.OOMKilled}}')" != '0|false' ]; then
    FAILED=1
fi
done
printf 'evidence=%s\nvalidation_exit=%s\n' "$ROOT_OUT" "$FAILED"
exit "$FAILED"
