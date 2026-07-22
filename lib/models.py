# SPDX-FileCopyrightText: 2026 Andrew Gunnerson
# SPDX-License-Identifier: GPL-3.0-only

from typing import ClassVar
import typing

from pydantic import BaseModel, ConfigDict


class OtaPartitionState(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    partition_name: str
    device: list[str]
    build: list[str]
    version: str


class OtaDeviceState(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    device: list[str]
    build: list[str]
    build_incremental: str
    timestamp: int
    sdk_level: str
    security_patch_level: str
    partition_state: list[OtaPartitionState]


class OtaMetadata(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    type: str
    wipe: bool
    downgrade: bool
    property_files: dict[str, str]
    precondition: OtaDeviceState
    postcondition: OtaDeviceState
    required_cache: int
    spl_downgrade: bool


class OtaInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    files: list[str]
    metadata: OtaMetadata


class PayloadPartition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    partition_name: str
    run_postinstall: bool | None = None
    postinstall_path: str | None = None
    filesystem_type: str | None = None
    postinstall_optional: bool | None = None
    version: str | None = None


class PayloadDynamicPartitionGroup(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    name: str
    size: int | None = None
    partition_names: list[str] = []


class PayloadVABCFeatureSet(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    threaded: bool | None = None
    batch_writes: bool | None = None


class PayloadDynamicPartitionMetadata(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    groups: list[PayloadDynamicPartitionGroup] = []
    snapshot_enabled: bool | None = None
    vabc_enabled: bool | None = None
    vabc_compression_param: str | None = None
    cow_version: int | None = None
    vabc_feature_set: PayloadVABCFeatureSet | None = None
    compression_factor: int | None = None
    disable_ublk: bool | None = None


class PayloadApexInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    package_name: str | None = None
    version: int | None = None
    is_compressed: bool | None = None
    decompressed_size: int | None = None


class PayloadManifest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    block_size: int | None = None
    minor_version: int | None = None
    partitions: list[PayloadPartition] = []
    max_timestamp: int | None = None
    dynamic_partition_metadata: PayloadDynamicPartitionMetadata | None = None
    partial_update: bool | None = None
    apex_info: list[PayloadApexInfo] = []
    security_patch_level: str | None = None


class PayloadInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    version: int
    manifest: PayloadManifest


class LpPartition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    name: str
    attributes: str


class LpPartitionGroup(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    name: str
    flags: str
    maximum_size: int | None = None
    partitions: list[LpPartition]


class LpBlockDevice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    first_logical_sector: int
    alignment: int
    alignment_offset: int
    size: int
    partition_name: str
    flags: str


class LpMetadataSlot(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    major_version: int
    minor_version: int
    groups: list[LpPartitionGroup]
    block_devices: list[LpBlockDevice]
    flags: str


class LpMetadata(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='forbid')

    image_type: typing.Literal['Normal', 'Empty']
    metadata_max_size: int
    metadata_slot_count: int
    logical_block_size: int
    slots: list[LpMetadataSlot]
