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
TASK=axe-step03b-install-$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')
OUT=validation/step03b/evidence-$TASK
/bin/mkdir "$OUT"
printf '%s\n' "$TASK" > "$OUT/task.txt"
for path in validation validation/step03b validation/step03b/Dockerfile.dependencies validation/step03b/install_dependencies.py validation/step03b/wheels-9f40a34c-0d6a-463c-95fe-8dc0a9ec2b2a validation/step03b/evidence-axe-step03b-5c3b9969-06d6-4960-afeb-c2e6db8d9f97/wheel-manifest.json; do
    [ ! -L "$path" ]
done
# GNU long-name records support wheel filenames beyond USTAR's 100-byte basename limit.
/usr/bin/tar --format=gnutar -cf "$OUT/context.tar" \
    validation/step03b/Dockerfile.dependencies validation/step03b/install_dependencies.py \
    validation/step03b/wheels-9f40a34c-0d6a-463c-95fe-8dc0a9ec2b2a \
    validation/step03b/evidence-axe-step03b-5c3b9969-06d6-4960-afeb-c2e6db8d9f97/wheel-manifest.json
docker_clean build --platform linux/arm64 --pull=false --network none --rm=false --force-rm=false \
    -f validation/step03b/Dockerfile.dependencies -t "$TASK:local" - < "$OUT/context.tar" > "$OUT/build.log" 2>&1
IMAGE=$(docker_clean image inspect "$TASK:local" --format '{{.Id}}')
docker_clean volume create "$TASK-scratch" > "$OUT/volume.txt"
CID=$(docker_clean create --name "$TASK" --pull never --platform linux/arm64 --network none \
    --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --ipc none \
    --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 --log-driver local \
    --log-opt max-size=1m --log-opt max-file=1 --log-opt compress=false \
    --mount "type=volume,source=$TASK-scratch,target=/scratch" "$IMAGE")
docker_clean inspect "$CID" > "$OUT/inspect-before.json"
actual=$(docker_clean inspect "$CID" --format '{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidMode}}|{{.HostConfig.IpcMode}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{len .Mounts}}|{{len .HostConfig.Devices}}|{{len .HostConfig.PortBindings}}|{{with index .HostConfig "Tmpfs"}}{{len .}}{{else}}0{{end}}')
[ "$actual" = 'true|10001:10001|none|false||none|1000000000|536870912|536870912|64|["ALL"]|["no-new-privileges:true"]|1|0|0|0' ]
[ "$(docker_clean inspect "$CID" --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}|{{.RW}}{{end}}')" = "volume|$TASK-scratch|/scratch|true" ]
[ "$(docker_clean inspect "$CID" --format '{{.Image}}')" = "$IMAGE" ]
docker_clean start "$CID"
SECONDS=0
TIMED_OUT=false
while [ "$(docker_clean inspect "$CID" --format '{{.State.Status}}')" = running ]; do
    if [ "$SECONDS" -ge 240 ]; then
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
[ "$TIMED_OUT" = false ]
[ "$(docker_clean inspect "$CID" --format '{{.State.ExitCode}}|{{.State.OOMKilled}}')" = '0|false' ]
docker_clean cp "$CID:/scratch/install-report.json" "$OUT/install-report.json"
printf 'dependency_install=passed\n'
