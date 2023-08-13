#!/usr/bin/python3
# Copyright 2018 David O'Rourke <david.orourke@gmail.com>
# Copyright 2022 MakeMHz LLC <contact@makemhz.com>
# Based on ciso from https://github.com/jamie/ciso

import os
import struct
import sys
import lz4.frame

CISO_MAGIC = 0x4F534943 # CISO
CISO_HEADER_SIZE = 0x18 # 24
CISO_BLOCK_SIZE = 0x800 # 2048
CISO_HEADER_FMT = '<LLQLBBxx' # Little endian
CISO_PLAIN_BLOCK = 0x80000000

TITLE_MAX_LENGTH = 40

#assert(struct.calcsize(CISO_HEADER_FMT) == CISO_HEADER_SIZE)

image_offset = 0

def get_terminal_size(fd=sys.stdout.fileno()):
	try:
		import fcntl, termios
		hw = struct.unpack("hh", fcntl.ioctl(
			fd, termios.TIOCGWINSZ, '1234'))
	except:
		try:
			hw = (os.environ['LINES'], os.environ['COLUMNS'])
		except:
			hw = (25, 80)
	return hw

(console_height, console_width) = get_terminal_size()

def update_progress(progress):
	barLength = console_width - len("Progress: 100% []") - 1
	block = int(round(barLength*progress)) + 1
	text = "\rProgress: [{blocks}] {percent:.0f}%".format(
			blocks="#" * block + "-" * (barLength - block),
			percent=progress * 100)
	sys.stdout.write(text)
	sys.stdout.flush()

def check_file_size(f):
	global image_offset

	f.seek(0, os.SEEK_END)
	file_size = f.tell() - image_offset
	ciso = {
			'magic': CISO_MAGIC,
			'ver': 2,
			'block_size': CISO_BLOCK_SIZE,
			'total_bytes': file_size,
			'total_blocks': int(file_size / CISO_BLOCK_SIZE),
			'align': 2,
			}
	f.seek(image_offset, os.SEEK_SET)
	return ciso

def write_cso_header(f, ciso):
	f.write(struct.pack(CISO_HEADER_FMT,
		ciso['magic'],
		CISO_HEADER_SIZE,
		ciso['total_bytes'],
		ciso['block_size'],
		ciso['ver'],
		ciso['align']
		))

def write_block_index(f, block_index):
	for index, block in enumerate(block_index):
		try:
			f.write(struct.pack('<I', block))
		except Exception as e:
			print("Writing block={} with data={} failed.".format(
				index, block))
			print(e)
			sys.exit(1)

def detect_iso_type(f):
	global image_offset

	# Detect if the image is a REDUMP image
	f.seek(0x18310000)
	buffer = f.read(20)
	if buffer == b"MICROSOFT*XBOX*MEDIA":
		print("REDUMP image detected")
		image_offset = 0x18300000
		return

	# Detect if the image is a raw XDVDFS image
	f.seek(0x10000)
	buffer = f.read(20)
	if buffer == b"MICROSOFT*XBOX*MEDIA":
		image_offset = 0
		return

	# Print error and exit
	print("ERROR: Could not detect ISO type.")
	sys.exit(1)

# Pad file size to ATA block size * 2
def pad_file_size(f):
	f.seek(0, os.SEEK_END)
	size = f.tell()
	f.write(struct.pack('<B', 0x00) * (0x400 - (size & 0x3FF)))

def compress_iso(infile):
	lz4_context = lz4.frame.create_compression_context()

	# Replace file extension with .cso
	fout_1 = open(os.path.splitext(infile)[0] + '.1.cso', 'wb')
	fout_2 = None

	with open(infile, 'rb') as fin:
		print("Compressing '{}'".format(infile))

		# Detect and validate the ISO
		detect_iso_type(fin)

		ciso = check_file_size(fin)
		for k, v in ciso.items():
			print("{}: {}".format(k, v))

		write_cso_header(fout_1, ciso)
		block_index = [0x00] * (ciso['total_blocks'] + 1)

		# Write the dummy block index for now.
		write_block_index(fout_1, block_index)

		write_pos = fout_1.tell()
		align_b = 1 << ciso['align']
		align_m = align_b - 1

		# Alignment buffer is unsigned char.
		alignment_buffer = struct.pack('<B', 0x00) * 64

		# Progress counters
		percent_period = ciso['total_blocks'] / 100
		percent_cnt = 0

		split_fout = fout_1

		for block in range(0, ciso['total_blocks']):
			# Check if we need to split the ISO (due to FATX limitations)
			# TODO: Determine a better value for this.
			if write_pos > 0xFFBF6000:
				# Create new file for the split
				fout_2     = open(os.path.splitext(infile)[0] + '.2.cso', 'wb')
				split_fout = fout_2

				# Reset write position
				write_pos  = 0

			# Write alignment
			align = int(write_pos & align_m)
			if align:
				align = align_b - align
				size = split_fout.write(alignment_buffer[:align])
				write_pos += align

			# Mark offset index
			block_index[block] = write_pos >> ciso['align']

			# Read raw data
			raw_data = fin.read(ciso['block_size'])
			raw_data_size = len(raw_data)

			# Compress block
			# Compressed data will have the gzip header on it, we strip that.
			lz4.frame.compress_begin(lz4_context, compression_level=lz4.frame.COMPRESSIONLEVEL_MAX,
				auto_flush=True, content_checksum=False, block_checksum=False, block_linked=False, source_size=False)

			compressed_data = lz4.frame.compress_chunk(lz4_context, raw_data, return_bytearray=True)
			compressed_size = len(compressed_data)

			lz4.frame.compress_flush(lz4_context)

			# Ensure compressed data is smaller than raw data
			# TODO: Find optimal block size to avoid fragmentation
			if (compressed_size + 12) >= raw_data_size:
				writable_data = raw_data

				# Next index
				write_pos += raw_data_size
			else:
				writable_data = compressed_data

				# LZ4 block marker
				block_index[block] |= 0x80000000

				# Next index
				write_pos += compressed_size

			# Write data
			split_fout.write(writable_data)

			# Progress bar
			percent = int(round((block / (ciso['total_blocks'] + 1)) * 100))
			if percent > percent_cnt:
				update_progress((block / (ciso['total_blocks'] + 1)))
				percent_cnt = percent

		# TODO: Pad file to ATA block size

		# end for block
		# last position (total size)
		# NOTE: We don't actually need this, but we're keeping it for legacy reasons.
		block_index[-1] = write_pos >> ciso['align']

		# write header and index block
		print("\nWriting block index")
		fout_1.seek(CISO_HEADER_SIZE, os.SEEK_SET)
		write_block_index(fout_1, block_index)

	# end open(infile)
	pad_file_size(fout_1)
	fout_1.close()

	if fout_2:
		pad_file_size(fout_2)
		fout_2.close()

# https://www.caustik.com/cxbx/download/xbe.htm
def get_xbe_title_offset(in_file, offset = 0):
	base_addr_offset  = offset + 0x104
	cert_addr_offset  = offset + 0x118
	title_addr_offset = 0xc

	with open(in_file, 'rb') as f:
		f.seek(base_addr_offset)
		base_addr = struct.unpack('<I', f.read(4))[0]

		f.seek(cert_addr_offset)
		cert_addr = struct.unpack('<I', f.read(4))[0]

	return offset + (cert_addr - base_addr + title_addr_offset)

def is_xbe_file(xbe):
	if not os.path.isfile(xbe):
		return False

	with open(xbe, 'rb') as xbe_file:
		magic = xbe_file.read(4)

		if magic != b'XBEH':
			return False

	return True

def get_default_xbe_file_offset_in_iso(iso_file):
	return get_file_offset_in_iso(iso_file, 'default.xbe')

# only looks in root dir
def get_file_offset_in_iso(iso_file, search_file):
	global image_offset

	sector_size          = 0x800
	iso_header_offset    = 0x10000
	iso_header_magic_len = 20
	filename_offset      = 14

	with open(iso_file, 'rb') as f:
		detect_iso_type(f)

		# seek to root dir
		f.seek(image_offset + iso_header_offset + iso_header_magic_len)
		root_dir_offset = image_offset + struct.unpack('<I', f.read(4))[0] * sector_size
		f.seek(root_dir_offset)

		l_offset = 0
		dword    = 4

		# loop through root dir entries
		while True:
			l_offset = struct.unpack('<H', f.read(2))[0]

			# end of dir entries
			if l_offset == 0xffff:
				break

			r_offset     = struct.unpack('<H', f.read(2))[0]
			start_sector = struct.unpack('<I', f.read(4))[0]
			file_size    = struct.unpack('<I', f.read(4))[0]
			attribs      = struct.unpack('<B', f.read(1))[0]
			filename_len = struct.unpack('<B', f.read(1))[0]
			filename     = f.read(filename_len).decode('utf-8')

			#print("entry:", format(l_offset), format(r_offset), format(start_sector), format(file_size), format(attribs), format(filename_len), filename)

			# entries are aligned on 4 byte bounderies
			next_offset = (dword - ((filename_offset + filename_len) % dword)) % dword
			f.seek(next_offset, os.SEEK_CUR)

			# our file was found, return the abs offset
			if filename == search_file:
				return image_offset + start_sector * sector_size

	# entry wasn't found
	return 0

def read_default_xbe_title_from_iso(iso_file):
	xbe_offset = get_default_xbe_file_offset_in_iso(iso_file)

	if xbe_offset == 0:
		return os.path.splitext(os.path.basename(iso_file))[0]

	title_offset = get_xbe_title_offset(iso_file, xbe_offset)

	with open(iso_file, 'rb') as f:
		f.seek(title_offset)
		title_bytes = f.read(TITLE_MAX_LENGTH * 2).decode('utf-16-le')
		return title_bytes

def gen_attach_xbe(iso_file):
	in_file_name  = os.path.dirname(os.path.abspath(__file__)) + '/attach_cso.xbe'
	out_file_name = os.path.dirname(os.path.abspath(iso_file)) + '/default.xbe'

	if not is_xbe_file(in_file_name):
		return

	title_offset = get_xbe_title_offset(in_file_name)

	title = read_default_xbe_title_from_iso(iso_file)
	title = title[0:TITLE_MAX_LENGTH]

	print("Generating default.xbe with title:", title)

	with open(in_file_name, 'rb') as in_xbe:
		with open(out_file_name, 'wb') as out_xbe:
			before = in_xbe.read(title_offset)
			in_xbe.seek(TITLE_MAX_LENGTH * 2, os.SEEK_CUR)
			after = in_xbe.read()

			title = title.ljust(TITLE_MAX_LENGTH, "\x00")
			title_bytes = title.encode('utf-16-le')

			out_xbe.write(before)
			out_xbe.write(title_bytes)
			out_xbe.write(after)

def main(argv):
	infile = argv[1]
	compress_iso(infile)
	gen_attach_xbe(infile)

if __name__ == '__main__':
	sys.exit(main(sys.argv))
