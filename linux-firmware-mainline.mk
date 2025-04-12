#
# SPDX-FileCopyrightText: The LineageOS Project
# SPDX-License-Identifier: Apache-2.0
#

LOCAL_FIRMWARE_PATH := external/linux-firmware-mainline/firmware

$(foreach fw,$(TARGET_ODM_FIRMWARE_COPY),\
    $(eval PRODUCT_COPY_FILES += $(LOCAL_FIRMWARE_PATH)/$(fw):$(TARGET_COPY_OUT_ODM)/firmware/$(fw)))

$(foreach fw,$(TARGET_VENDOR_FIRMWARE_COPY),\
    $(eval PRODUCT_COPY_FILES += $(LOCAL_FIRMWARE_PATH)/$(fw):$(TARGET_COPY_OUT_VENDOR)/firmware/$(fw)))
