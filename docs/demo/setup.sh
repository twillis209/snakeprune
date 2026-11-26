#!/usr/bin/env bash
# Build the throwaway pipeline that demo.tape records: bash setup.sh DIR
set -euo pipefail
d=${1:?usage: setup.sh DIR}
rm -rf "$d"; mkdir -p "$d"; cd "$d"
cat > Snakefile <<'SMK'
SAMPLES = ["A", "B", "C"]
REPS = ["1", "2"]

rule all:
    input:
        expand("results/{sample}/{rep}/tpm/10/{f}",
               sample=SAMPLES, rep=REPS, f=["counts.tsv", "qc.txt"]),

rule count:
    output:
        "results/{sample}/{rep}/{norm}/{min_count}/counts.tsv",
    shell:
        "echo {wildcards} > {output}"

rule qc:
    input:
        "results/{sample}/{rep}/{norm}/{min_count}/counts.tsv",
    output:
        "results/{sample}/{rep}/{norm}/{min_count}/qc.txt",
    shell:
        "wc -l {input} > {output}"
SMK
snakemake -q -c1 >/dev/null 2>&1
# Leftovers from earlier iterations of the output layout
for s in A B; do for r in 1 2; do echo old > "results/$s/$r/counts.tsv"; done; done
for r in 1 2; do mkdir -p "results/A/$r/cpm"; echo old > "results/A/$r/cpm/counts.tsv"; done
rm -rf .snakemake  # no lock or metadata left to trip the scan
