## Test Result
model: 2 layer GCN

hidden dim: 16

dataset: Reddit

| | GPU | CPU 1 worker | CPU 4 worker |
|  ----  | ----  | ---- | ---- |
| dataloader  | 1.72, 1.50 | 1.66, 6.67 | 1.62, 2.20 |
| feature index | 7.48, 0.35 | 9.56, 0.60 | 9.83, 0.75 |
| feature to GPU | 6.48, 0.13 | 6.65, 0.13 | 7.22, 0.18 |
| graph to GPU| 0.30, 0.30 | 0.78, 0.79 | 0.84, 1.03 |
| inference | 0.98, 0.77 | 0.98, 0.85 | 1.03, 1.08 |
| feature to CPU | 0.05, 0.05 | 0.09, 0.17 | 0.09, 0.21 |
| total | 20.12 | 29.22 | 26.41 |

When dataloader device set GPU, the tensor of input node's index, graph, and output node's index is already in GPU.