# GPU runs

An opt-in extension of the PR integration workflow that adds a second static
confidential CRN (an NVIDIA H200 in CC mode) and runs a CUDA V-PROGRAM
end to end: `vprogram create --gpu hopper`, scheduler placement, and attested
calls into the guest over RA-TLS.

Skipped everywhere else: `tests/test_vprograms_gpu.py` skips unless
`ALEPH_TESTNET_NVIDIA_CC_CRN_HOST` is set, which only happens on a
GPU-flagged run. A default `pull_request` run or a `workflow_dispatch`
without `gpu=true` never touches the GPU host.

## Dispatching a GPU run

```
gh workflow run pr-tests.yml \
  -f gpu=true \
  -f pyaleph_tag=0.12.0-rc0 \
  -f aleph_vm_branch=... \
  -f aleph_cli_url=...
```

`pyaleph_tag` currently must be `0.12.0-rc0` to get GPU V-PROGRAM support;
the manifesto's default pyaleph pin does not have it yet. `aleph_vm_branch`
and `aleph_cli_url` are optional, same as any other dispatch run: they
override the manifesto's aleph-vm branch and CLI download URL.

## Secrets and variables

Set on the `digitalocean` environment, alongside the existing TEE server
ones:

- `NVIDIA_CC_HOST` (the GPU host's address)
- `NVIDIA_CC_USER` (SSH login, `root`)
- `NVIDIA_CC_IPV6_POOL`, a repository variable (the GPU host's IPv6 pool
  override, applied via `crn-up.sh`)

The CI SSH key already authorised on the TEE server must also be authorised
on the GPU host.

### The IPv6 pool

The scheduler refuses to place V-PROGRAMs on a node whose advertised IPv6
pool is a ULA range, so `NVIDIA_CC_IPV6_POOL` must be a public-looking /64,
not `fd00::/8` or similar. Until the GPU host has a routed /64 from its
provider, use the discard-only prefix as a stopgap:

```
NVIDIA_CC_IPV6_POOL=100::/64
```

This satisfies the scheduler's placement check without claiming a real
route; the GPU test itself does no IPv6 reachability probe. Replace it with
the host's real routed /64 once the provider attaches one.

## Host prerequisites

- SEV-SNP enabled in firmware
- The GPU card in confidential-compute mode and VFIO-bound
- Docker (`docker.io`): static CRNs skip the base-package install, and the
  vm-connector runs as a container
- QEMU >= 9.1 on `PATH` (Ubuntu 24.04's packaged QEMU 8.2 cannot launch SNP
  guests with a GPU attached; a newer build must live somewhere `PATH`
  resolves it, e.g. `/usr/local/bin/qemu-system-x86_64`)
- `vfio_iommu_type1 dma_entry_limit` raised enough for the workload's DMA
  mappings

## What the test does

`test_gpu_vprogram_deploy_and_attested_cuda_call` creates a CUDA V-PROGRAM
with `--gpu hopper`, waits for it to become reachable, checks the scheduler
placed it on the NVIDIA CC host (not the plain SNP TEE server), checks
`vprogram show` reports the pinned `gpu` requirement
(`{"vendor":"nvidia","arch":"hopper","count":1,"mode":"cc"}`), makes attested
calls to `/gpu` and `/bandwidth` on the guest, and finally creates a second
V-PROGRAM requesting `--gpu blackwell` to confirm the scheduler leaves it
unplaced (no Blackwell card exists on this host). Both V-PROGRAMs are
deleted in a `finally` block regardless of outcome.
