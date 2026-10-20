`checksums: true` with `partition_by` on the three changeset writers (#205): a checksum per
  partition, stored in `_agentl_checksums`, skips partitions equal to the last run without
  comparing their rows. The result reports `partitions_skipped`.
