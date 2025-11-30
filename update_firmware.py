#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: The LineageOS Project
# SPDX-License-Identifier: Apache-2.0
#

from argparse import ArgumentParser
from pathlib import Path
from re import match
from shutil import copyfile, rmtree
import shlex
import subprocess
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional, Set

LINUX_FIRMWARE_REPO = "https://gitlab.com/kernel-firmware/linux-firmware"
LINUX_FIRMWARE_BRANCH = "main"

SCRIPT_PATH = Path(__file__).resolve().parent
DESTINATION_PATH = SCRIPT_PATH / "firmware"
ANDROID_BP_PATH = DESTINATION_PATH / "Android.bp"

def make_aosp_target_name(name: str) -> str:
	"""
	Convert an identifier or file path to an AOSP target name.
	"""
	conversions = {
		"/": "_",
		" ": "-",
		",": "_",
	}
	for old, new in conversions.items():
		name = name.replace(old, new)

	return f"linux_firmware_{name}"

class Blob:
	"""
	A firmware file.

	If links_to is not None, this file is a symlink to another firmware file.
	"""
	def __init__(
		self,
		path: Path,
		links_to: Optional[Path],
		version: Optional[str],
		info: Optional[str],
	) -> None:
		self.path = path
		self.links_to = links_to
		self.version = version
		self.info = info

	def get_aosp_target_name(self) -> str:
		"""
		Return the AOSP target name for this firmware file.
		"""
		# Replace non-alphanumeric characters with underscores
		safe_path = "_".join(
			part
			for part
			in self.path.parts
		)

		return make_aosp_target_name(safe_path)

class License:
	def __init__(
		self,
		lines: List[str],
	) -> None:
		self.lines = lines

	def extend(
		self,
		other: "License",
	) -> None:
		"""
		Extend this license with another license (used for merging licenses).
		"""
		self.lines.extend(other.lines)

	def get_referenced_files(self) -> Set[str]:
		"""
		Return a set of files referenced in this license.
		"""
		referenced_files: Set[str] = set()

		for line in self.lines:
			if line.startswith("See "):
				parts = line[len("See "):].strip().split(",", 1)
				file_path = parts[0].strip()
				referenced_files.add(file_path)

		return referenced_files

class WhenceEntry:
	def __init__(
		self,
		driver_name: str,
		driver_description: Optional[str],
		license_to_blobs: Dict[License, List[Blob]],
	) -> None:
		self.driver_name = driver_name
		self.driver_description = driver_description
		self.license_to_blobs = license_to_blobs

	def extend(
		self,
		other: "WhenceEntry",
	) -> None:
		"""
		Extend this entry with another entry (used for merging entries with the same driver name).
		"""
		assert self.driver_name == other.driver_name, \
			"Cannot extend entries with different driver names"

		for license, files in other.license_to_blobs.items():
			if license in self.license_to_blobs:
				self.license_to_blobs[license].extend(files)
			else:
				self.license_to_blobs[license] = files

	def get_aosp_target_name(self) -> str:
		"""
		Return the AOSP target name for this entry.
		"""
		return make_aosp_target_name(self.driver_name)

def parse_whence(file: Path) -> Dict[str, WhenceEntry]:
	"""
	Parse the WHENCE file and return a list of WhenceEntry objects.

	If two entries have the same driver name, they are grouped together in the returned dictionary.
	"""
	entries: Dict[str, WhenceEntry] = {}

	# Entry variables
	driver_name: Optional[str] = None
	driver_description: Optional[str] = None
	blobs: List[Blob] = []
	license: Optional[License] = None

	# Firmware file variables
	firmware_path: Optional[str] = None
	firmware_links_to: Optional[str] = None
	firmware_version: Optional[str] = None
	firmware_info: Optional[str] = None

	# License variables
	license_lines: List[str] = []
	in_license_block = False

	def handle_license():
		nonlocal license_lines, license, in_license_block

		if in_license_block:
			# Remove the last lines if they are empty
			while license_lines and not license_lines[-1].strip():
				license_lines.pop()

			assert len(license_lines) > 0, "License block must not be empty"

			license = License(lines=license_lines)

		# Reset license variables
		license_lines = []
		in_license_block = False

	def handle_firmware_file():
		nonlocal firmware_path, firmware_links_to, firmware_version, firmware_info, blobs

		if firmware_path:
			firmware_path = shlex.split(firmware_path)[0]

			if firmware_links_to:
				firmware_links_to = shlex.split(firmware_links_to)[0]

			if " " in firmware_path or (firmware_links_to and " " in firmware_links_to):
				print(f"Warning: Skipping firmware file with spaces in path: {firmware_path}")
			else:
				blob = Blob(
					path=Path(firmware_path),
					links_to=Path(firmware_links_to) if firmware_links_to else None,
					version=firmware_version,
					info=firmware_info,
				)
				blobs.append(blob)

		# Reset the variables
		firmware_path = None
		firmware_links_to = None
		firmware_version = None
		firmware_info = None

	def handle_entry():
		nonlocal driver_name, driver_description, blobs, license, entries

		# Handle license first
		handle_license()

		# Handle the last firmware file if it exists
		handle_firmware_file()

		if driver_name:
			assert driver_name is not None, "Driver name must not be null"
			assert len(blobs) > 0, "Each entry must have at least one blob"
			assert license is not None, "License must not be null"

			entry = WhenceEntry(
				driver_name=driver_name,
				driver_description=driver_description,
				license_to_blobs={license: blobs},
			)

			if driver_name in entries:
				entries[driver_name].extend(entry)
			else:
				entries[driver_name] = entry

		# Reset for next entry
		driver_name = None
		driver_description = None
		blobs = []
		license = None

	for line in file.read_text().splitlines():
		if not line.strip():
			# Licenses are interrupted by empty lines
			handle_license()

			continue

		if line.startswith("Driver:"):
			parts = line[len("Driver:"):].strip().split(" - ", 1)
			driver_name = parts[0].strip()
			if len(parts) > 1:
				driver_description = parts[1].strip()
		elif line.startswith("File:"):
			# Add the previous firmware file if it exists
			handle_firmware_file()

			firmware_path = line[len("File:"):].strip()
		elif line.startswith("RawFile:"):
			# Add the previous firmware file if it exists
			handle_firmware_file()

			firmware_path = line[len("RawFile:"):].strip()
		elif line.startswith("Link:"):
			# Add the previous firmware file if it exists
			handle_firmware_file()

			firmware_path, firmware_links_to = [
				path.strip()
				for path
				in line[len("Link:"):].strip().split("->", 1)
			]
		elif line.startswith("Version:"):
			firmware_version = line[len("Version:"):].strip()
		elif line.startswith("Info:"):
			firmware_info = line[len("Info:"):].strip()
		elif line.startswith("License:") or line.startswith("Licence:"):
			in_license_block = True

			# len("License:") == len("Licence:")
			license_lines.append(line[len("License:"):].strip())
		elif line.startswith("-"):
			# Finalize the current entry
			handle_entry()
		else:
			# Random info
			pass

	return entries

def find_dependencies_for_entry(
	entry: WhenceEntry,
	entries: Dict[str, WhenceEntry],
) -> Set[str]:
	"""
	Some entries depend on firmware files provided by other entries via symlinks.
	This function finds all such dependencies for a given entry.
	"""
	dependencies: Set[str] = set()

	for file in entry.license_to_blobs.values():
		for firmware_file in file:
			if not firmware_file.links_to:
				continue

			for other_entry_name, other_entry in entries.items():
				if other_entry is entry:
					continue

				for other_files in other_entry.license_to_blobs.values():
					for other_firmware_file in other_files:
						if other_firmware_file.path != firmware_file.links_to:
							continue

						dependencies.add(other_entry_name)
						break

	return dependencies

def main():
	arg_parser = ArgumentParser(
		description="Update firmware files from the linux-firmware repository",
	)
	arg_parser.add_argument(
		"--firmware-path",
		default=None,
		type=Path,
		help="Path to the firmware directory, will be cloned if not provided",
	)

	args = arg_parser.parse_args()

	firmware_path = args.firmware_path

	entries: Dict[str, WhenceEntry] = {}

	with TemporaryDirectory() as temp_dir:
		if not firmware_path:
			print(f"Cloning {LINUX_FIRMWARE_REPO} into temporary directory...")
			subprocess.run(
				[
					"git", "clone",
					"--branch", LINUX_FIRMWARE_BRANCH,
					"--depth", "1",
					"--single-branch",
					"--no-tags",
					LINUX_FIRMWARE_REPO,
					temp_dir,
				],
				check=True,
			)

			firmware_path = Path(temp_dir)

		# Parse the WHENCE file
		whence_file = firmware_path / "WHENCE"
		assert whence_file.exists(), "WHENCE file not found in the cloned repository"

		entries = parse_whence(whence_file)

		print("Removing existing firmware files...")
		if DESTINATION_PATH.exists():
			rmtree(DESTINATION_PATH)

		print("Updating firmware files...")
		for entry_name, entry in entries.items():
			for license, blobs in entry.license_to_blobs.items():
				for blob in blobs:
					src_path = firmware_path / blob.path
					dest_path = DESTINATION_PATH / blob.path

					assert not dest_path.exists(), f"Destination path {dest_path} already exists"

					if not blob.links_to:
						# Ensure the destination directory exists
						dest_path.parent.mkdir(parents=True, exist_ok=True)

						# Copy the file
						copyfile(src_path, dest_path)

		# Write Android.bp file
		print(f"Writing {ANDROID_BP_PATH}...")
		with ANDROID_BP_PATH.open("w") as f:
			f.write("//\n")
			f.write("// SPDX-FileCopyrightText: The LineageOS Project\n")
			f.write("// SPDX-License-Identifier: Apache-2.0\n")
			f.write("//\n")
			f.write("\n")
			f.write("// Auto-generated with update_firmware.py\n")

			all_entry_targets: Set[str] = set()
			for entry_name, entry in entries.items():
				entry_aosp_target_name = entry.get_aosp_target_name()

				all_files: List[Path] = []
				all_symlink_targets: Set[str] = set()

				for _, blobs in entry.license_to_blobs.items():
					for blob in blobs:
						if blob.links_to:
							target_name = blob.get_aosp_target_name()

							all_symlink_targets.add(target_name)

							f.write(f'\n')
							f.write(f'install_symlink {{\n')
							f.write(f'    name: "{target_name}",\n')
							f.write(f'    installed_location: "{blob.path}",\n')
							f.write(f'    symlink_target: "{blob.links_to}",\n')
							f.write(f'    soc_specific: true,\n')
							f.write(f'    visibility: ["//external/linux-firmware-mainline:__subpackages__"],\n')
							f.write(f"}}\n")
						else:
							all_files.append(blob.path)

				if all_files:
					f.write(f'\n')
					f.write(f'prebuilt_firmware {{\n')
					f.write(f'    name: "{entry_aosp_target_name}",\n')
					f.write(f'    srcs: [\n')
					for file in all_files:
						f.write(f'        "{file.as_posix()}",\n')
					f.write(f'    ],\n')
					f.write(f'    dsts: [\n')
					for file in all_files:
						f.write(f'        "{file.as_posix()}",\n')
					f.write(f'    ],\n')
					f.write(f'    soc_specific: true,\n')
					if all_symlink_targets:
						f.write(f'    required: [\n')
						for symlink_target in sorted(all_symlink_targets):
							f.write(f'        "{symlink_target}",\n')
						f.write(f'    ],\n')
					f.write(f'    visibility: ["//external/linux-firmware-mainline:__subpackages__"],\n')
					f.write(f"}}\n")
				elif all_symlink_targets:
					# Make a phony target that copies all symlinks
					f.write(f'\n')
					f.write(f'phony {{\n')
					f.write(f'    name: "{entry_aosp_target_name}",\n')
					f.write(f'    required: [\n')
					for symlink_target in sorted(all_symlink_targets):
						f.write(f'        "{symlink_target}",\n')
					f.write(f'    ],\n')
					f.write(f'    visibility: ["//external/linux-firmware-mainline:__subpackages__"],\n')
					f.write(f'}}\n')

				if all_files or all_symlink_targets:
					all_entry_targets.add(entry_aosp_target_name)

			# Phony target for all firmware
			f.write(f'\n')
			f.write(f'phony {{\n')
			f.write(f'    name: "linux_firmware_all",\n')
			f.write(f'    required: [\n')
			for target in sorted(all_entry_targets):
				f.write(f'        "{target}",\n')
			f.write(f'    ],\n')
			f.write(f'}}\n')

if __name__ == "__main__":
	main()
