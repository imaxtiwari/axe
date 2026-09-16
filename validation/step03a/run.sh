#!/bin/bash
set -euo pipefail

trap 'rc=$?; printf "STOP: command failed, exit=%s; resources retained\n" "$rc" >&2; exit "$rc"' ERR

CLI=/Applications/Docker.app/Contents/Resources/bin/docker
BASE=docker.io/library/python@sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9
DIR=validation/step03a

[ -x "$CLI" ]
[ -S /var/run/docker.sock ]
[ -d /var/empty ]
[ ! -L /var/empty ]
for path in /var/empty/config.json /var/empty/contexts /var/empty/cli-plugins; do
    [ ! -e "$path" ] && [ ! -L "$path" ]
done
for path in validation "$DIR"; do
    [ -d "$path" ] && [ ! -L "$path" ]
done
for path in "$DIR/Dockerfile" "$DIR/selftest.py" "$DIR/run.sh"; do
    [ -f "$path" ] && [ ! -L "$path" ]
done

docker_clean() {
    /usr/bin/env -i PATH=/usr/bin:/bin HOME=/var/empty \
        DOCKER_CONFIG=/var/empty DOCKER_BUILDKIT=0 \
        "$CLI" --config /var/empty --host unix:///var/run/docker.sock "$@"
}

docker_clean image inspect "$BASE" \
    --format 'base_id={{.Id}} os={{.Os}} arch={{.Architecture}}'

TOKEN=$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')
TASK="axe-step03a-$TOKEN"
VOLUME="$TASK-scratch"
IMAGE="$TASK:local"
CONTAINER="$TASK-test"
printf 'task=%s\nvolume=%s\nimage=%s\ncontainer=%s\n' \
    "$TASK" "$VOLUME" "$IMAGE" "$CONTAINER"

existing=$(docker_clean volume ls --quiet --filter "name=^${VOLUME}$")
[ -z "$existing" ]
existing=$(docker_clean container ls --all --quiet --filter "name=^/${CONTAINER}$")
[ -z "$existing" ]
existing=$(docker_clean image ls --quiet --filter "reference=$IMAGE")
[ -z "$existing" ]

/usr/bin/tar --format=ustar -c -f - -C "$DIR" Dockerfile selftest.py | \
    docker_clean build --platform linux/arm64 --pull=false --network none --rm=false --force-rm=false \
        --label "axe.validation.task=$TASK" --tag "$IMAGE" -

IMAGE_ID=$(docker_clean image inspect "$IMAGE" --format '{{.Id}}')
docker_clean volume create --label "axe.validation.task=$TASK" "$VOLUME"

CID=$(docker_clean create \
    --name "$CONTAINER" \
    --label "axe.validation.task=$TASK" \
    --pull never \
    --platform linux/arm64 \
    --network none \
    --read-only \
    --user 10001:10001 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --ipc none \
    --cpus 1 \
    --memory 512m \
    --memory-swap 512m \
    --pids-limit 64 \
    --log-driver local \
    --log-opt max-size=1m \
    --log-opt max-file=1 \
    --log-opt compress=false \
    --mount "type=volume,source=$VOLUME,target=/scratch" \
    "$IMAGE")

# Inspect this task-owned container before starting it.
# These assertions cover the principal runtime restrictions.
actual=$(docker_clean inspect "$CID" --format \
'{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidMode}}|{{.HostConfig.IpcMode}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{len .Mounts}}|{{len .HostConfig.Devices}}|{{len .HostConfig.PortBindings}}|{{with index .HostConfig "Tmpfs"}}{{len .}}{{else}}0{{end}}')
expected='true|10001:10001|none|false||none|1000000000|536870912|536870912|64|["ALL"]|["no-new-privileges:true"]|1|0|0|0'
if [ "$actual" != "$expected" ]; then
    printf 'STOP: unexpected runtime configuration\nexpected=%s\nactual=%s\n' \
        "$expected" "$actual" >&2
    exit 1
fi

actual=$(docker_clean inspect "$CID" --format \
'{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}|{{.RW}}{{end}}')
[ "$actual" = "volume|$VOLUME|/scratch|true" ]

actual=$(docker_clean inspect "$CID" --format '{{.Image}}')
[ "$actual" = "$IMAGE_ID" ]

printf 'runtime_configuration=passed\n'
docker_clean start "$CID"

SECONDS=0
while :; do
    state=$(docker_clean inspect "$CID" --format '{{.State.Status}}')
    case "$state" in
        exited)
            break
            ;;
        running)
            if [ "$SECONDS" -ge 60 ]; then
                printf 'STOP: supervisor deadline reached\n' >&2
                docker_clean stop --time 2 "$CID"
                docker_clean logs "$CID"
                printf 'container_exit_code='
                docker_clean inspect "$CID" --format '{{.State.ExitCode}}'
                exit 124
            fi
            /bin/sleep 1
            ;;
        *)
            printf 'STOP: unexpected container state: %s\n' "$state" >&2
            exit 1
            ;;
    esac
done

exit_code=$(docker_clean inspect "$CID" --format '{{.State.ExitCode}}')
oom_killed=$(docker_clean inspect "$CID" --format '{{.State.OOMKilled}}')

printf 'container=%s\nimage=%s\nvolume=%s\n' \
    "$CONTAINER" "$IMAGE_ID" "$VOLUME"
docker_clean logs "$CID"
printf 'container_exit_code=%s\noom_killed=%s\n' "$exit_code" "$oom_killed"

case "$exit_code" in
    ''|*[!0-9]*)
        printf 'STOP: invalid container exit code\n' >&2
        exit 1
        ;;
esac
if [ "$exit_code" -ne 0 ]; then
    exit "$exit_code"
fi
if [ "$oom_killed" != false ]; then
    printf 'STOP: container was OOM-killed\n' >&2
    exit 1
fi
printf 'synthetic_run=passed\n'
