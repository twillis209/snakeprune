#!/usr/bin/env bash
# Build the throwaway pipeline that make_gif.py records: bash setup.sh DIR
set -euo pipefail
d=${1:?usage: setup.sh DIR}
rm -rf "$d"; mkdir -p "$d"; cd "$d"
cat > Snakefile <<'SMK'
SAMPLES = ["liver_rep1", "liver_rep2", "heart_rep1", "heart_rep2"]

rule all:
    input:
        expand("results/counts/{sample}/star/tpm/{f}",
               sample=SAMPLES, f=["counts.tsv", "qc_metrics.txt"]),
        "results/diffexp/liver_vs_heart/star/tpm/deseq2_results.tsv",

rule quantify:
    output:
        "results/counts/{sample}/{aligner}/{norm}/counts.tsv",
    shell:
        "echo {wildcards} > {output}"

rule qc:
    input:
        "results/counts/{sample}/{aligner}/{norm}/counts.tsv",
    output:
        "results/counts/{sample}/{aligner}/{norm}/qc_metrics.txt",
    shell:
        "wc -l {input} > {output}"

rule diffexp:
    input:
        expand("results/counts/{sample}/{{aligner}}/{{norm}}/counts.tsv",
               sample=SAMPLES),
    output:
        "results/diffexp/liver_vs_heart/{aligner}/{norm}/deseq2_results.tsv",
    shell:
        "cat {input} > {output}"
SMK
snakemake -q -c1 >/dev/null 2>&1
# Leftovers from earlier iterations of the output layout: before the
# aligner level, then before the normalisation level.
# Sizes are realistic so the --summary size column looks like a real run.
old() { head -c "$1" /dev/zero > "$2"; }
old 1843200 results/counts/liver_rep1/counts.tsv
old 1789952 results/counts/liver_rep2/counts.tsv
old 2117632 results/counts/heart_rep1/star/counts.tsv
old 2050048 results/counts/heart_rep2/star/counts.tsv
old 663552 results/diffexp/liver_vs_heart/star/deseq2_results.tsv
rm -rf .snakemake  # no lock or metadata left to trip the scan
