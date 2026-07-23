#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Andrew Gunnerson
# SPDX-License-Identifier: GPL-3.0-only

import argparse
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import typing
import zipfile

import tomlkit

from lib.apex_manifest_pb2 import ApexManifest
from lib.models import (
    LpMetadata, OtaDeviceState, OtaInfo, OtaMetadata, OtaPartitionState,
    PayloadApexInfo, PayloadDynamicPartitionGroup,
    PayloadDynamicPartitionMetadata, PayloadInfo, PayloadManifest,
    PayloadPartition,
)
from lib.ota_metadata_pb2 import ApexInfo, ApexMetadata


class PartialFile:
    def __init__(self, file: typing.BinaryIO, start: int, size: int):
        self.file: typing.BinaryIO = file
        self.start: int = start
        self.size: int = size
        self.pos: int = 0

        self.file.seek(start, os.SEEK_SET)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.size - self.pos
        size = max(size, 0)

        data = self.file.read(size)
        self.pos += len(data)

        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET):
        if whence == os.SEEK_SET:
            new_pos = offset
        elif whence == os.SEEK_CUR:
            new_pos = self.pos + offset
        elif whence == os.SEEK_END:
            new_pos = self.size + offset
        else:
            raise ValueError(f'Invalid whence: {whence}')

        if new_pos < 0:
            raise ValueError(f'Negative offset: {new_pos}')

        new_raw_pos = self.start + new_pos
        raw_pos = self.file.seek(new_raw_pos, os.SEEK_SET)
        if raw_pos != new_raw_pos:
            raise OSError(f'seek failed: {raw_pos} != {new_raw_pos}')

        self.pos = new_pos

        return new_pos

    def seekable(self) -> bool:
        return True

    def tell(self):
        return self.pos


def cmd(*args, cwd = pathlib.Path.cwd()):
    # Make all paths absolute so that they work regardless of which directory
    # we're running from.
    args = list(args)
    for i, arg in enumerate(args):
        if isinstance(arg, pathlib.Path):
            args[i] = arg.absolute()
    cwd = cwd.absolute()

    cmd = ' '.join(shlex.quote(str(arg)) for arg in args)
    print(f'Running: {cmd} [cwd: {cwd}]', file=sys.stderr)

    subprocess.check_call(args, cwd=cwd)


def load_toml(path: pathlib.Path) -> dict[str, typing.Any]:
    with open(path, 'r') as f:
        return tomlkit.load(f)


def save_toml(path: pathlib.Path, data: dict[str, typing.Any]):
    with open(path, 'w') as f:
        tomlkit.dump(data, f)


def extract_factory_image(
    factory: pathlib.Path,
    images_dir: pathlib.Path,
    super_dir: pathlib.Path,
):
    with open(factory, 'rb') as f:
        image_zip_size = None
        image_zip_offset = None

        with zipfile.ZipFile(f, 'r') as z:
            for info in z.infolist():
                name = pathlib.Path(info.filename).name

                if not name.startswith('image-') or not name.endswith('.zip'):
                    continue
                elif info.compress_type != zipfile.ZIP_STORED:
                    raise ValueError(f'{name!r} is not uncompressed')

                image_zip_size = info.file_size
                image_zip_offset = info._end_offset - image_zip_size

        if image_zip_offset is None or image_zip_size is None:
            raise ValueError('"image-*.zip" not found in factory zip')

        f_image_zip = PartialFile(f, image_zip_offset, image_zip_size)

        with zipfile.ZipFile(f_image_zip, 'r') as z:
            for info in z.infolist():
                name = pathlib.Path(info.filename).name

                if name in (
                    'android-info.txt',
                    'fastboot-info.txt',
                    'kernel_16k',
                    'ramdisk_16k.img',
                    'system_other.img',
                    'userdata_exp.ai.img',
                ):
                    continue

                if name == 'super_empty.img':
                    output_dir = super_dir
                else:
                    output_dir = images_dir

                output_file = output_dir / name
                if output_file.exists():
                    continue

                print(f'Extracting from factory image: {name}')

                try:
                    with (
                        z.open(info, 'r') as f_in,
                        open(output_dir / name, 'wb') as f_out,
                    ):
                        shutil.copyfileobj(f_in, f_out)
                except Exception as e:
                    output_file.unlink(missing_ok=True)
                    raise e


def parse_dynamic_partitions(super_toml: pathlib.Path) -> PayloadDynamicPartitionGroup:
    super_info = LpMetadata.model_validate(load_toml(super_toml))

    for group in super_info.slots[0].groups:
        if group.name != 'default':
            partitions: list[str] = []

            for partition in group.partitions:
                if pathlib.Path(partition.name).name != partition.name:
                    raise ValueError(f'Unsafe name: {partition.name}')

                partitions.append(partition.name.removesuffix('_a'))

            return PayloadDynamicPartitionGroup(
                name=group.name.removesuffix('_a'),
                size=group.maximum_size,
                partition_names=partitions,
            )

    raise ValueError(f'Missing dynamic partition group: {super_info}')


def parse_build_prop(path: pathlib.Path) -> dict[str, str]:
    result: dict[str, str] = {}

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith('#'):
                continue

            key, delim, value = line.partition('=')
            if not delim:
                raise ValueError(f'Invalid build.prop line: {line!r}')

            result[key] = value

    return result


def parse_build_props(
    filesystems: pathlib.Path,
    dpg: PayloadDynamicPartitionGroup,
) -> dict[str, dict[str, str]]:
    props: dict[str, dict[str, str]] = {}

    for name in dpg.partition_names:
        fs_tree = filesystems / name / 'fs_tree'

        if name == 'system':
            build_prop_path = fs_tree / 'system' / 'build.prop'
        elif name == 'vendor':
            build_prop_path = fs_tree / 'build.prop'
        else:
            build_prop_path = fs_tree / 'etc' / 'build.prop'

        props[name] = parse_build_prop(build_prop_path)

    return props


def parse_apex(path: pathlib.Path) -> PayloadApexInfo:
    apex_info = PayloadApexInfo()

    with zipfile.ZipFile(path, 'r') as z:
        for info in z.infolist():
            if info.filename == 'original_apex':
                apex_info.is_compressed = True
                apex_info.decompressed_size = info.file_size
            elif info.filename == 'apex_manifest.pb':
                with z.open(info, 'r') as f:
                    manifest = ApexManifest()
                    manifest.ParseFromString(f.read())

                    apex_info.package_name = manifest.name
                    apex_info.version = manifest.version

    return apex_info


def parse_apex_infos(
    filesystems: pathlib.Path,
    dpg: PayloadDynamicPartitionGroup,
) -> list[PayloadApexInfo]:
    apex_infos: list[PayloadApexInfo] = []

    for name in dpg.partition_names:
        fs_tree = filesystems / name / 'fs_tree'

        if name == 'system':
            apex_dir = fs_tree / 'system' / 'apex'
        else:
            apex_dir = fs_tree / 'apex'

        if not apex_dir.exists():
            continue

        for apex in sorted(
            a for a in apex_dir.iterdir()
            if a.name.endswith('.apex') or a.name.endswith('.capex')
        ):
            apex_infos.append(parse_apex(apex))

    return apex_infos


def create_payload_toml(
    payload_images: pathlib.Path,
    dpg: PayloadDynamicPartitionGroup,
    build_props: dict[str, dict[str, str]],
    apex_infos: list[PayloadApexInfo],
    payload_toml: pathlib.Path,
):
    partitions = [i.name.removesuffix('.img') for i in payload_images.glob('*.img')]
    partitions.sort()

    info = PayloadInfo(version=2, manifest=PayloadManifest())
    info.manifest.block_size = 4096
    info.manifest.minor_version = 0
    info.manifest.security_patch_level = \
        build_props['system']['ro.build.version.security_patch']

    for name in partitions:
        partition = PayloadPartition(partition_name=name)

        if name in dpg.partition_names:
            partition.version = build_props[name][f'ro.{name}.build.date.utc']

        # We can only guess based on what AOSP does.
        if name == 'system':
            partition.run_postinstall = True
            partition.postinstall_path = 'system/bin/otapreopt_script'
            partition.filesystem_type = 'ext4'
            partition.postinstall_optional = True
        elif name == 'vendor':
            partition.run_postinstall = True
            partition.postinstall_path = 'bin/checkpoint_gc'
            partition.filesystem_type = 'ext4'
            partition.postinstall_optional = True

        info.manifest.partitions.append(partition)

    info.manifest.max_timestamp = \
        max(int(p.version) for p in info.manifest.partitions if p.version)

    info.manifest.dynamic_partition_metadata = PayloadDynamicPartitionMetadata(
        snapshot_enabled=True,
        vabc_enabled=True,
        vabc_compression_param='lz4',
        cow_version=3,
        compression_factor=65536,
        groups=[dpg],
    )

    info.manifest.apex_info = apex_infos

    save_toml(payload_toml, info.model_dump(exclude_none=True))


def create_apex_info(
    apex_infos: list[PayloadApexInfo],
    apex_info_pb: pathlib.Path,
):
    metadata = ApexMetadata()

    for payload_apex_info in apex_infos:
        apex_info = ApexInfo()
        apex_info.package_name = payload_apex_info.package_name
        apex_info.version = payload_apex_info.version
        apex_info.is_compressed = payload_apex_info.is_compressed or False
        apex_info.decompressed_size = payload_apex_info.decompressed_size or 0

        metadata.apex_info.append(apex_info)

    with open(apex_info_pb, 'wb') as f:
        f.write(metadata.SerializeToString())


def create_ota_toml(
    dpg: PayloadDynamicPartitionGroup,
    build_props: dict[str, dict[str, str]],
    ota_toml: pathlib.Path,
):
    codename = build_props['vendor']['ro.product.vendor.device']

    info = OtaInfo(
        # We can't generate care_map.pb, but it's not required anyway.
        files=['apex_info.pb', 'payload.bin', 'payload_properties.txt'],
        metadata=OtaMetadata(
            type="AB",
            wipe=False,
            downgrade=False,
            property_files={},
            precondition=OtaDeviceState(
                device=[codename],
                build=[],
                build_incremental='',
                timestamp=0,
                sdk_level='',
                security_patch_level='',
                partition_state=[],
            ),
            postcondition=OtaDeviceState(
                device=[codename],
                build=[build_props['vendor']['ro.vendor.build.fingerprint']],
                build_incremental=build_props['system']['ro.build.version.incremental'],
                timestamp=int(build_props['system']['ro.build.date.utc']),
                sdk_level=build_props['system']['ro.build.version.sdk'],
                security_patch_level=build_props['system']['ro.build.version.security_patch'],
                partition_state=[],
            ),
            required_cache=0,
            spl_downgrade=False,
        ),
    )

    for name in sorted(dpg.partition_names):
        info.metadata.postcondition.partition_state.append(OtaPartitionState(
            partition_name=name,
            device=[build_props[name][f'ro.product.{name}.device']],
            build=[build_props[name][f'ro.{name}.build.fingerprint']],
            version=build_props[name][f'ro.{name}.build.date.utc'],
        ))

    save_toml(ota_toml, info.model_dump(exclude_none=True))


def generate_ota(
    input_factory: pathlib.Path,
    output_ota: pathlib.Path,
    work_dir: pathlib.Path,
):
    ota_dir = work_dir / 'ota'
    ota_dir.mkdir(parents=True, exist_ok=True)

    ota_files = ota_dir / 'ota_files'
    ota_files.mkdir(exist_ok=True)

    payload_images = ota_dir / 'payload_images'
    payload_images.mkdir(exist_ok=True)

    super_dir = work_dir / 'super'
    super_dir.mkdir(exist_ok=True)

    filesystems = work_dir / 'filesystems'
    filesystems.mkdir(exist_ok=True)

    # We only need the images from the nested image-*.zip within the factory
    # zip. It includes the separated bootloader and modem partitions so it is
    # not necessary to unpack Google's proprietary fbpack format from
    # bootloader-*.img and radio-*.img.
    extract_factory_image(input_factory, payload_images, super_dir)

    # The dynamic partition group metadata is needed to create the payload.
    super_toml = super_dir / 'lp.toml'
    if not super_toml.exists():
        cmd('avbroot', 'lp', 'unpack', '-q', '-i', 'super_empty.img', cwd=super_dir)

    dpg = parse_dynamic_partitions(super_toml)

    for name in dpg.partition_names:
        extracted = filesystems / name
        extracted.mkdir(exist_ok=True)

        if not (extracted / 'fs_metadata.toml').exists():
            cmd('afsr', 'unpack', '-i', payload_images / f'{name}.img', cwd=extracted)

    build_props = parse_build_props(filesystems, dpg)
    apex_infos = parse_apex_infos(filesystems, dpg)

    payload_toml = ota_dir / 'payload.toml'
    create_payload_toml(
        payload_images,
        dpg,
        build_props,
        apex_infos,
        payload_toml,
    )

    ota_toml = ota_dir / 'ota.toml'
    create_ota_toml(dpg, build_props, ota_toml)

    apex_info_pb = ota_files / 'apex_info.pb'
    create_apex_info(apex_infos, apex_info_pb)

    # We intentionally use an ephemeral signing keypair. The output is not meant
    # to be flashed. It must be patched with avbroot first.
    ota_key = work_dir / 'ota.key'
    ota_cert = work_dir / 'ota.crt'

    if not ota_key.exists():
        cmd(
            'avbroot', 'key', 'generate-key',
            '-t', 'rsa4096',
            '-o', ota_key,
            '--pass-file', os.devnull,
        )
    if not ota_cert.exists():
        cmd(
            'avbroot', 'key', 'generate-cert',
            '-k', ota_key,
            '-o', ota_cert,
        )

    # Pixel devices include bootloader partitions in the AVB metadata, so
    # verifying ensures they were not omitted.
    cmd('avbroot', 'avb', 'verify', '-i', payload_images / 'vbmeta.img')

    # Create the OTA!
    cmd(
        'avbroot', 'zip', 'pack', '-q',
        '-o', output_ota,
        '-k', ota_key,
        '-c', ota_cert,
        '--payload',
        cwd=ota_dir,
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-i', '--input',
        required=True,
        type=pathlib.Path,
        help='Input factory image',
    )
    parser.add_argument(
        '-o', '--output',
        required=True,
        type=pathlib.Path,
        help='Output OTA',
    )
    parser.add_argument(
        '-w', '--work-dir',
        type=pathlib.Path,
        help='Working directory (for debugging)',
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.work_dir is not None:
        generate_ota(args.input, args.output, args.work_dir)
    else:
        with tempfile.TemporaryDirectory() as work_dir:
            generate_ota(args.input, args.output, pathlib.Path(work_dir))


if __name__ == '__main__':
    main()
