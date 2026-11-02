# Running ClearPath on Kubernetes (local kind cluster)

The same container image that runs on Cloud Run, deployed with plain manifests: a namespace, a ConfigMap,
a two-replica Deployment with readiness and liveness probes, resource limits and a non-root security
context, and a ClusterIP Service. It runs in offline demo mode, so no API keys are needed.

```bash
kind create cluster --name clearpath
docker build -t clearpath:local .
kind load docker-image clearpath:local --name clearpath
kubectl apply -f k8s/clearpath.yaml
kubectl -n clearpath rollout status deploy/clearpath
kubectl -n clearpath port-forward svc/clearpath 8080:80   # then open http://localhost:8080
```

Rolling update with no downtime (maxUnavailable 0):

```bash
kubectl -n clearpath rollout restart deploy/clearpath
kubectl -n clearpath rollout status deploy/clearpath
```
