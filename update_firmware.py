#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: The LineageOS Project
# SPDX-License-Identifier: Apache-2.0
#

from argparse import ArgumentParser
from os.path import normpath
from pathlib import Path
import re
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
		return make_aosp_target_name(self.path.as_posix())

class License:
	def __init__(
		self,
		lines: Optional[List[str]] = None,
	) -> None:
		self.lines = lines or []

	def add_line(
		self,
		line: str,
	) -> None:
		"""
		Add a line to this license.
		"""
		self.lines.append(line)

	def get_referenced_files(self) -> Set[str]:
		"""
		Return a set of license files referenced in this license.
		"""
		referenced_files: Set[str] = set()

		match_found = False
		for line in self.lines:
			result = re.match(r'.*See (.*) for details.*', line, flags=re.IGNORECASE)
			if result:
				for file in re.split(r", | and ", result.group(1)):
					match_found = True
					referenced_files.add(file.strip())

		# No known match, return it anyway if it's a single not known word
		if not match_found and len(self.lines) == 1:
			single_line = self.lines[0].strip()
			if re.match(r'^\S+$', single_line):
				if not re.match(r'unknown|distributable', single_line, flags=re.IGNORECASE):
					referenced_files.add(single_line)
					match_found = True

		return referenced_files

class Driver:
	"""
	A driver mentioned in the WHENCE file.
	"""
	def __init__(
		self,
		name: str,
		description: Optional[str],
		license_to_blobs: Dict[License, List[Blob]],
	) -> None:
		self.name = name
		self.description = description
		self.license_to_blobs = license_to_blobs

	def extend(
		self,
		other: "Driver",
	) -> None:
		"""
		Extend this driver with another driver of the same name.
		"""
		assert self.name == other.name, \
			"Cannot extend drivers with different driver names"

		for license, files in other.license_to_blobs.items():
			if license in self.license_to_blobs:
				self.license_to_blobs[license].extend(files)
			else:
				self.license_to_blobs[license] = files

	def get_aosp_target_name(self) -> str:
		"""
		Return the AOSP target name for this driver.
		"""
		return make_aosp_target_name(self.name)

def parse_whence(file: Path) -> Dict[str, Driver]:
	"""
	Parse the WHENCE file and return a list of Driver objects.

	If two drivers have the same name, they are grouped together in the returned dictionary.
	"""
	drivers: Dict[str, Driver] = {}

	# Driver variables
	driver_name: Optional[str] = None
	driver_description: Optional[str] = None
	driver_license_to_blobs: Dict[License, List[Blob]] = {}

	# License variables
	blobs: List[Blob] = []
	license = License()
	in_license_block = False

	# Blob variables
	blob_path: Optional[str] = None
	blob_links_to: Optional[str] = None
	blob_version: Optional[str] = None
	blob_info: Optional[str] = None

	def handle_blob():
		"""
		Called when we encounter a new blob, driver, or reach the end of the file.
		Finalizes the current blob and adds it to the blobs list.
		"""
		nonlocal blob_path, blob_links_to, blob_version, blob_info, blobs

		if blob_path:
			blob_path = shlex.split(blob_path)[0]

			if blob_links_to:
				blob_links_to = shlex.split(blob_links_to)[0]

			if " " in blob_path or (blob_links_to and " " in blob_links_to):
				print(f"Warning: Skipping blobs with spaces in path: {blob_path}")
			else:
				blob = Blob(
					path=Path(blob_path),
					links_to=Path(blob_links_to) if blob_links_to else None,
					version=blob_version,
					info=blob_info,
				)
				blobs.append(blob)

		# Reset the variables
		blob_path = None
		blob_links_to = None
		blob_version = None
		blob_info = None

	def handle_license():
		"""
		Called when a license declaration is finished or we encounter a new driver.
		Finalizes the current license block and adds it to the licenses list.
		"""
		nonlocal license, blobs, driver_license_to_blobs

		assert len(license.lines) > 0, "License block must not be empty"

		# Associate blobs with this license
		if blobs:
			driver_license_to_blobs[license] = blobs
			blobs = []

		# Reset license variables
		license = License()

	def handle_driver():
		"""
		Called when we encounter a new driver or reach the end of the file.
		Finalizes the current driver and adds it to the drivers list.
		"""
		nonlocal drivers, driver_name, driver_description, driver_license_to_blobs, blobs

		if driver_name:
			# Handle the last blob if it exists
			handle_blob()

			# Associate blobs with this license
			if blobs:
				driver_license_to_blobs[license] = blobs
				blobs = []

			assert driver_name is not None, "Driver name must not be null"
			assert len(driver_license_to_blobs) > 0, "Each driver must have at least one license"
			for _, b in driver_license_to_blobs.items():
				assert b, "Each license must have at least one blob"

			driver = Driver(
				name=driver_name,
				description=driver_description,
				license_to_blobs=driver_license_to_blobs,
			)

			if driver_name in drivers:
				drivers[driver_name].extend(driver)
			else:
				drivers[driver_name] = driver

		# Reset for next driver
		driver_name = None
		driver_description = None
		driver_license_to_blobs = {}

	for line in file.read_text().splitlines():
		if not line.strip():
			# Empty line
			if in_license_block:
				in_license_block = False
				handle_license()
		elif line.startswith("Driver:"):
			# Finalize the current driver
			handle_driver()

			parts = line[len("Driver:"):].strip().split(" - ", 1)
			driver_name = parts[0].strip()
			if len(parts) > 1:
				driver_description = parts[1].strip()
		elif line.startswith("File:"):
			# Add the previous firmware file if it exists
			handle_blob()

			blob_path = line[len("File:"):].strip()
		elif line.startswith("RawFile:"):
			# Add the previous firmware file if it exists
			handle_blob()

			blob_path = line[len("RawFile:"):].strip()
		elif line.startswith("Link:"):
			# Add the previous firmware file if it exists
			handle_blob()

			blob_path, blob_links_to = [
				path.strip()
				for path
				in line[len("Link:"):].strip().split("->", 1)
			]
		elif line.startswith("Version:"):
			blob_version = line[len("Version:"):].strip()
		elif line.startswith("Info:"):
			blob_info = line[len("Info:"):].strip()
		elif line.startswith("License:") or line.startswith("Licence:"):
			# Add the previous firmware file if it exists
			handle_blob()

			in_license_block = True

			# len("License:") == len("Licence:")
			license.add_line(line[len("License:"):].strip())
		elif line.startswith("--"):
			# Section end
			if in_license_block:
				in_license_block = False
				handle_license()
		else:
			# Random info
			if in_license_block:
				license.add_line(line)

	# Handle the last entry
	handle_driver()

	return drivers

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

	drivers: Dict[str, Driver] = {}

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

		# Make sure we have a git repository and get commit title + hash
		command_result = subprocess.run(
			["git", "log", "-1", "--pretty=format:%H"],
			cwd=firmware_path,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
		)
		assert command_result.returncode == 0, \
			"Failed to get latest commit from firmware repository"

		commit_info = command_result.stdout.strip()

		# Parse the WHENCE file
		whence_file = firmware_path / "WHENCE"
		assert whence_file.exists(), "WHENCE file not found in the cloned repository"

		drivers = parse_whence(whence_file)

		print("Removing existing firmware files...")
		if DESTINATION_PATH.exists():
			rmtree(DESTINATION_PATH)

		print("Updating firmware files...")
		for _, driver in drivers.items():
			for license, blobs in driver.license_to_blobs.items():
				for blob in blobs:
					src_path = firmware_path / blob.path
					dest_path = DESTINATION_PATH / blob.path

					assert not dest_path.exists(), f"Destination path {dest_path} already exists"

					if not blob.links_to:
						# Ensure the destination directory exists
						dest_path.parent.mkdir(parents=True, exist_ok=True)

						# Copy the file
						copyfile(src_path, dest_path)

		print("Copying license files...")
		for _, driver in drivers.items():
			for license, _ in driver.license_to_blobs.items():
				for license_file in license.get_referenced_files():
					src_path = firmware_path / license_file
					dest_path = DESTINATION_PATH / license_file

					if dest_path.exists():
						continue

					# Ensure the destination directory exists
					dest_path.parent.mkdir(parents=True, exist_ok=True)

					# Copy the file
					copyfile(src_path, dest_path)

	# Make a dict of blob path to blob for easy lookup
	blob_path_to_blob: Dict[Path, Blob] = {}
	for _, driver in drivers.items():
		for _, blobs in driver.license_to_blobs.items():
			for blob in blobs:
				blob_path_to_blob[blob.path] = blob

	# Write Android.bp file
	print(f"Writing Android.bp...")
	with ANDROID_BP_PATH.open("w") as f:
		f.write("//\n")
		f.write("// SPDX-FileCopyrightText: The LineageOS Project\n")
		f.write("// SPDX-License-Identifier: Apache-2.0\n")
		f.write("//\n")
		f.write("\n")
		f.write("// Auto-generated with update_firmware.py\n")
		f.write(f"// Source commit: {commit_info}\n")

		all_first_level_targets: Set[str] = set()
		for _, driver in drivers.items():
			driver_aosp_target_name = driver.get_aosp_target_name()

			all_driver_targets: Set[str] = set()

			for _, blobs in driver.license_to_blobs.items():
				for blob in blobs:
					target_name = blob.get_aosp_target_name()

					all_driver_targets.add(target_name)

					f.write(f'\n')
					if blob.links_to:
						required_target: Optional[str] = None
						resolved_path = Path(
							normpath((blob.path.parent / blob.links_to).as_posix())
						)
						if resolved_path in blob_path_to_blob:
							required_target = blob_path_to_blob[resolved_path].get_aosp_target_name()
						else:
							print(
								f"Warning: {resolved_path} is not a known blob (most likely a"
								f" directory link), cannot set required target for {blob.path}"
							)

						f.write(f'install_symlink {{\n')
						f.write(f'    name: "{target_name}",\n')
						f.write(f'    installed_location: "firmware/{blob.path.as_posix()}",\n')
						f.write(f'    symlink_target: "{blob.links_to.as_posix()}",\n')
						if required_target:
							f.write(f'    required: [\n')
							f.write(f'        "{required_target}",\n')
							f.write(f'    ],\n')
						f.write(f'    soc_specific: true,\n')
						f.write(f'    visibility: ["//external/linux-firmware-mainline:__subpackages__"],\n')
						f.write(f"}}\n")
					else:
						f.write(f'prebuilt_firmware {{\n')
						f.write(f'    name: "{target_name}",\n')
						f.write(f'    src: "{blob.path.as_posix()}",\n')
						f.write(f'    sub_dir: "{blob.path.parent.as_posix()}",\n')
						f.write(f'    filename_from_src: true,\n')
						f.write(f'    soc_specific: true,\n')
						f.write(f'    visibility: ["//external/linux-firmware-mainline:__subpackages__"],\n')
						f.write(f"}}\n")

			if all_driver_targets:
				# Make a phony target that copies all driver files and symlinks
				f.write(f'\n')
				f.write(f'phony {{\n')
				f.write(f'    name: "{driver_aosp_target_name}",\n')
				f.write(f'    required: [\n')
				for symlink_target in sorted(all_driver_targets):
					f.write(f'        "{symlink_target}",\n')
				f.write(f'    ],\n')
				f.write(f'    visibility: ["//external/linux-firmware-mainline:__subpackages__"],\n')
				f.write(f'}}\n')

				all_first_level_targets.add(driver_aosp_target_name)

		# Phony target for all firmware
		f.write(f'\n')
		f.write(f'phony {{\n')
		f.write(f'    name: "linux_firmware_all",\n')
		f.write(f'    required: [\n')
		for target in sorted(all_first_level_targets):
			f.write(f'        "{target}",\n')
		f.write(f'    ],\n')
		f.write(f'}}\n')

if __name__ == "__main__":
	main()
