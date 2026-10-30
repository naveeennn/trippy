# Running Nemotron on Nebius with NVIDIA NIM

Goal: the agent's model runs on your own Nebius GPU, so the pitch can say the reasoning
runs on NVIDIA's stack on Nebius, with latency numbers to show for it.

## 1. GPU VM on Nebius
- Create a VM with one H100 (or bigger for the larger Nemotron models) using an Ubuntu image
  with NVIDIA drivers + container toolkit preinstalled.
- Open port 8000 to your IP only (security group). Don't expose it to the internet.

## 2. Start the NIM
1. Get an NGC API key at https://org.ngc.nvidia.com/setup/api-key
2. Find the Nemotron model on https://build.nvidia.com, open **Deploy → Docker**, and copy its
   exact `docker run` command. Image names and tags change, so copy rather than guess. It looks like:

```bash
export NGC_API_KEY=<key>
echo "$NGC_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin
docker run -d --gpus all --shm-size=16GB -e NGC_API_KEY \
  -v ~/.cache/nim:/opt/nim/.cache -p 8000:8000 \
  nvcr.io/nim/nvidia/<nemotron-model>:<tag>
```

3. Wait for the model to load, then check:

```bash
curl http://localhost:8000/v1/models
```

## 3. Point TripOwner at it
In `.env`:

```
LLM_BASE_URL=http://<vm-ip>:8000/v1
LLM_MODEL=<the "id" returned by /v1/models>
```

Restart the server. The dashboard badge shows the model, and "Agent (Nemotron)" becomes selectable.

## 4. Numbers worth showing judges
- Time per agent decision (one driver message → reply sent). Log it from `LLMAgent._think`.
- Tool-calling accuracy over the eval: `python -m tripowner.sim.eval --trips 30 --llm`.
- Cost: tokens per trip × GPU-hour price vs a hosted API.

## Fallback
If the VM isn't ready in time, any OpenAI-compatible hosted Nemotron endpoint works with the
same two env vars, and the agent falls back to rules automatically if the LLM errors mid-demo.
