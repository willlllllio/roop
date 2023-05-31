#!/usr/bin/env python3

import platform
import re
import sys
import traceback

import torch  # needs to be imported before onnx for GPU support to work
import argparse
import os
from pathlib import Path
from core.processor import process_video, process_img, get_face
from core.utils import is_img, detect_fps, create_video, add_audio, extract_frames, ensure, Timer, create_video_with_audio
import psutil
import cv2

# DEFAULT_FRAME_SUFFIX_ORG = "_org.png"
# DEFAULT_FRAME_SUFFIX_SWAPPED = "_swapped.png"

DEFAULT_FRAME_SUFFIX_ORG = ".png"
DEFAULT_FRAME_SUFFIX_SWAPPED = ".png"


def name_pattern(name: str, length: int = 5):
	return f"%0{length}d{name}"


def limit_resources(args):
	if args['max_memory'] >= 1:
		memory = args['max_memory'] * 1024 * 1024 * 1024
		if str(platform.system()).lower() == 'windows':
			import ctypes
			kernel32 = ctypes.windll.kernel32
			kernel32.SetProcessWorkingSetSize(-1, ctypes.c_size_t(memory), ctypes.c_size_t(memory))
		else:
			import resource
			try:
				resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
			except:
				traceback.print_exc()


def pre_check():
	import shutil
	import core.globals
	import torch
	if sys.version_info < (3, 8):
		quit(f'Python version is not supported - please upgrade to 3.8 or higher')
	if not shutil.which('ffmpeg'):
		quit('ffmpeg is not installed!')
	model_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'inswapper_128.onnx')
	if not os.path.isfile(model_path):
		quit('File "inswapper_128.onnx" does not exist!')
	if '--gpu' in sys.argv:
		CUDA_VERSION = torch.version.cuda
		CUDNN_VERSION = torch.backends.cudnn.version()

		if 'ROCMExecutionProvider' not in core.globals.providers:
			if not torch.cuda.is_available() or not CUDA_VERSION:
				quit("You are using --gpu flag but CUDA isn't available or properly installed on your system.")
			if CUDA_VERSION > '11.8':
				quit(f"CUDA version {CUDA_VERSION} is not supported - please downgrade to 11.8.")
			if CUDA_VERSION < '11.4':
				quit(f"CUDA version {CUDA_VERSION} is not supported - please upgrade to 11.8")
			if CUDNN_VERSION < 8220:
				quit(f"CUDNN version {CUDNN_VERSION} is not supported - please upgrade to 8.9.1")
			if CUDNN_VERSION > 8910:
				quit(f"CUDNN version {CUDNN_VERSION} is not supported - please downgrade to 8.9.1")
	else:
		core.globals.providers = ['CPUExecutionProvider']


def _frames(frame_paths: list[Path], output_dir: Path, org_suffix: str, swapped_suffix: str) \
		-> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]], list[tuple[Path, Path]]]:
	frames = []
	for i in frame_paths:
		outname = i.name[:-len(org_suffix)] + swapped_suffix
		outpath = output_dir / outname
		frames.append((i, outpath))

	todo = []
	done = []

	for i in frames:
		(done if i[1].exists() else todo).append(i)

	return frames, todo, done


def start_processing_cpu(face_img: Path, frame_paths: list[tuple[Path, Path]], load_own_model: bool, pool, procnum: int):
	n = max(len(frame_paths) // procnum, 1)
	processes = []
	for i in range(0, len(frame_paths), n):
		p = pool.apply_async(process_video, args = (face_img, frame_paths[i:i + n], load_own_model, False))
		processes.append(p)

	for p in processes:
		p.get()


def start_processing_gpu(face_img: Path, frame_paths: list[tuple[Path, Path]], load_own_model: bool, pool, procnum: int):
	if pool is not None and procnum > 1:
		return start_processing_cpu(face_img, frame_paths, True, pool, procnum)

	process_video(face_img, frame_paths, load_own_model, False)


def status(string):
	print("Status: " + string)


_leading_num_reg = re.compile("^(\d+)(?:[^\d]|$)")


def get_framepaths(frames_dir: Path, filename_suffix: str, ensure_continuous: bool = True) -> list[Path]:
	with os.scandir(frames_dir) as it:
		files = [i for i in it if i.is_file() and i.name.endswith(filename_suffix)]

	with_num = [(int(_leading_num_reg.search(file.name).group(1)), file) for file in files]
	with_num = sorted(with_num)
	if ensure_continuous:
		nums = [i[0] for i in with_num]
		ensure(nums == sorted(range(1, len(files) + 1)), c = ("expected continuous frames", nums))

	return [Path(i.path) for _, i in with_num]


def makedir(path: str | Path, exist_ok = False, parents: bool | int = False):
	# like Path.mkdir but if parents is int create at most [parents] folders
	# instead of possibly all back to /

	path = Path(path)
	if isinstance(parents, bool):
		path.mkdir(exist_ok = exist_ok, parents = parents)
	else:
		ensure(parents > 0, c = parents)
		_must_exist, _parents = path, parents
		while _parents:
			_must_exist = _must_exist.parent
			_parents -= 1

		if not _must_exist.exists():
			raise FileNotFoundError("parent dir not found", _must_exist, parents, path)

		path.mkdir(exist_ok = exist_ok, parents = True)


def output_args_replace(format_str: str, face_path: Path, source_path: Path):
	def rep(match: re.Match):
		name = match.group(1)
		if name == "src_bn":
			return source_path.name
		if name == "face_bn":
			return face_path.name
		if name == "src_bnc":
			return source_path.with_suffix("").name
		if name == "face_bnc":
			return face_path.with_suffix("").name

		raise ValueError(f"unsupported format name: {name!r}")

	return re.sub(r"{(\w+)}", rep, format_str)


def start(args):
	print("DON'T WORRY. IT'S NOT STUCK/CRASHED.\n" * 5)
	face_path = Path(args["face"])
	source_path = Path(args["source_vid"])
	if not face_path:
		return print("\n[WARNING] Please select an image containing a face.")

	if not face_path.is_file():
		return print("\n[WARNING] face_path not found", face_path)

	if not source_path:
		return print("\n[WARNING] Please select a video/image to swap face in.")

	if not source_path.exists():
		return print("\n[WARNING] source_path not found", source_path)

	name_suffix_org = args["name_suffix_org"]
	name_suffix_swapped = args["name_suffix_swapped"]

	ensure(not (args["output_vid_formatted"] and args["output_vid"]), c = "got both output_vid_formatted and output_vid")
	if args["output_vid_formatted"]:
		args["output_vid"] = _out = output_args_replace(args["output_vid_formatted"], face_path, source_path)
		print(f"using formatted output path: {str(_out)!r}")

	output_path = args["output_vid"]
	if output_path:
		output_path = Path(output_path)
		if output_path.is_dir():
			output_path = output_path / source_path.with_suffix(".swapped.mp4").name
			print(f"output_path is directory, saving to {str(output_path)!r}")
	else:
		output_path = source_path.with_suffix(".swapped.mp4")

	ensure(not output_path.exists(), c = ("output_path exists", output_path))

	vid_output_audio: bool = args["vid_output_audio"]
	output_path_plain = None
	if args["vid_output_plain"]:
		output_path_plain = output_path.with_suffix(".plain.mp4")
		ensure(not output_path_plain.exists(), c = ("output_path_plain exists", output_path_plain))

	ffmpeg = dict(ffmpeg = args["ffmpeg"], extra_args = ["-hide_banner", "-loglevel", "info"])

	with Timer("setgrad took {:.2f} secs"):
		import torch
		torch.set_grad_enabled(False)

	# test_face = get_face(cv2.imread(str(face_path)))
	# if not test_face:
	# 	print("\n[WARNING] No face detected in source image. Please try with another one.\n")
	# 	return

	if source_path.is_file():
		if is_img(source_path):
			process_img(face_path, source_path, output_path)
			status("swap successful!")
			return

		status("detecting video's FPS...")
		fps_src = args["fps_source"] if args["fps_source"] is not None else detect_fps(source_path)
		print("fps_src", fps_src)
	else:
		fps_src = args["fps_source"]
		ensure(fps_src, c = ("source_path is png sequence, manually passing --fps_source framerate argument required"))

	if args["work_dir"]:
		workdir = Path(args["work_dir"])
	elif args["work_dir_root"]:
		workdir = Path(args["work_dir_root"])
		workdir = workdir / f"{output_path.name}.tmp"
	else:
		workdir = output_path.with_name(output_path.name + ".tmp")

	fps_target: int = args["fps_target"]
	if not args['keep_fps'] and fps_src > fps_target:
		# source_fps_path = workdir / f"{source_path.name}_fps_{fps_target}.mp4"
		# print("converting to fps", fps_target)
		# set_fps(source_path, this_path, 30)
		# source_path, src_fps = this_path, 30
		fps_use = fps_target
		fps_swapped = fps_target
		print("limiting fps to", fps_use)
	else:
		# 	shutil.copy(source_path, output_dir)
		fps_use = None
		fps_swapped = fps_src

	if source_path.is_file():
		if args["frames_dir"]:
			in_frames_dir = Path(args["frames_dir"])
		else:
			_root = args["frames_dir_root"] or workdir
			in_frames_dir = _root / f"f_in__{source_path.name}__F{fps_use or 'srcfps'}"
			if in_frames_dir.exists():
				status(f"frames dir exists, not extracting again, assuming okay: {str(in_frames_dir)!r}")
			else:
				makedir(in_frames_dir, exist_ok = True, parents = 2)
				status(f"extracting frames to {str(in_frames_dir)!r}")
				extract_frames(source_path, in_frames_dir, fps_use, filename_pattern = name_pattern(name_suffix_org), **ffmpeg)
	else:
		print("using png sequence as source")
		ensure(source_path.is_dir())
		in_frames_dir = source_path

	in_frame_paths = get_framepaths(in_frames_dir, name_suffix_org)
	status(f"got {len(in_frame_paths)} frames total.")

	with Timer("swap took {:.2f} secs"):
		if args["swapped_dir"]:
			swapped_frames_dir = Path(args["swapped_dir"])
		else:
			_root = args["swapped_dir_root"] or workdir
			swapped_frames_dir = _root / f"f_swapped__{source_path.name}__F{fps_use or 'srcfps'}"
			makedir(swapped_frames_dir, exist_ok = True, parents = 2)

		fp_all, fp_todo, fp_done = _frames(in_frame_paths, swapped_frames_dir, name_suffix_org, name_suffix_swapped)
		ensure(fp_all, c = ("didn't find any frames", in_frame_paths))

		del_done = False
		if args["redo_swapped"] and len(fp_todo) != len(fp_all):
			del_done = True
			print(f"redoing {len(fp_done)} already completed of {len(fp_all)}")

		if args["redo_completed_swap"] and not fp_todo:
			print(f"all {len(fp_all)} completed, redoing all")
			del_done = True

		if del_done:
			for _, dst in fp_done:
				dst.unlink(missing_ok = False)

			fp_todo = fp_all
			fp_done = []

		if fp_todo:
			status(f"swapping {len(fp_todo)} frames of {len(fp_all)} total, {len(fp_done)} finished.")
			procs_cpu = args["parallel_cpu"]
			procs_gpu = args["parallel_gpu"]
			use_gpu = args["gpu"]
			print(f"{procs_cpu=} {procs_gpu=} {use_gpu=}")

			pool = None
			if use_gpu:
				print("running on GPU")
				if procs_gpu > 1:
					import multiprocessing.dummy as mp
					pool = mp.Pool(procs_gpu)
					start_processing_gpu(face_path, fp_todo, True, pool, procs_gpu)
				else:
					start_processing_gpu(face_path, fp_todo, False, None, procs_gpu)
			else:
				import multiprocessing as mp
				pool = mp.Pool(procs_cpu)
				start_processing_cpu(face_path, fp_todo, False, pool, procs_cpu)

			if pool is not None:
				pool.close()
				pool.join()
		else:
			status("skipping swapping, all finished already")

	swapped_pat = name_pattern(name_suffix_swapped)
	ffargs = dict(filename_pattern = swapped_pat, **ffmpeg)
	if vid_output_audio:
		if output_path_plain is not None:
			status(f"creating plain video with fps {fps_swapped} from {len(fp_all)} frames at {str(output_path_plain)!r} without any audio.")
			create_video(swapped_frames_dir, fps_swapped, output_path_plain, **ffargs)

		status(f"creating video with fps {fps_swapped} from {len(fp_all)} frames at {str(output_path)!r} with audio from {str(source_path)!r}")
		create_video_with_audio(swapped_frames_dir, fps_swapped, source_path, output_path, **ffargs)
	else:
		status(f"creating video with fps {fps_swapped} from {len(fp_all)} frames at {str(output_path)!r} without any audio.")
		create_video(swapped_frames_dir, fps_swapped, output_path, **ffargs)

	status("swap successful!")
	return


def make_parser():
	def num_arg(num: str):
		if "." in num or "e" in num:
			return float(num)
		return int(num)

	parser = argparse.ArgumentParser()
	parser.add_argument("-f", "--face", help = "use this face")
	parser.add_argument("-s", "--source_vid", help = "replace this face")
	parser.add_argument("-o", "--output_vid", help = "save output to this file")
	parser.add_argument("-O", "--output_vid_formatted", help = "save output to this file with {} formatting")

	parser.add_argument("--name_suffix_org", default = DEFAULT_FRAME_SUFFIX_ORG,
						help = "suffix (including extension) of original frame names")
	parser.add_argument("--name_suffix_swapped", default = DEFAULT_FRAME_SUFFIX_SWAPPED,
						help = "suffix (including extension) of original frame names")

	parser.add_argument("--gpu", dest = "gpu", action = "store_true",
						help = "use gpu")
	parser.add_argument("--keep_frames", action = "store_true",
						help = "keep frames directory")
	parser.add_argument("--keep_fps", action = "store_true",
						help = "maintain original fps")
	parser.add_argument("--fps_target", type = num_arg, default = 30,
						help = "target fps")

	parser.add_argument("--fps_source", type = num_arg,
						help = "source video fps")

	def existing_path(p: str):
		path = Path(p)
		if not path.exists():
			raise argparse.ArgumentTypeError(f"path not found: {p!r}")
		return path

	parser.add_argument("--frames_dir", type = existing_path, help = "source frames tmp dir")
	parser.add_argument("--frames_dir_root", type = existing_path, help = "source frames tmp root dir")

	parser.add_argument("--swapped_dir", type = existing_path, help = "swapped tmp dir")
	parser.add_argument("--swapped_dir_root", type = existing_path, help = "swapped tmp root dir")

	parser.add_argument("--work_dir", type = existing_path, help = "work tmp dir")
	parser.add_argument("--work_dir_root", type = existing_path, help = "work tmp root dir")

	parser.add_argument("--no_face_check", action = "store_false", dest = "face_check",
						help = "skip checking for face in first image")

	parser.add_argument("-A", "--no_audio", dest = "vid_output_audio", action = "store_false",
						help = "dont try to copy audio from source")
	parser.add_argument("-P", "--no_plain", dest = "vid_output_plain", action = "store_false",
						help = "dont create plaint output video (.plain.mp4 file without original audio)")

	parser.add_argument("-S", "--redo_swapped", action = "store_true",
						help = "always redo any already swapped images")

	parser.add_argument("-C", "--redo_completed_swap", action = "store_true",
						help = "redo swapping if it has been fully completed")

	parser.add_argument("--max_memory", default = 16, type = int, help = "set max memory")
	parser.add_argument("--parallel_cpu", type = int, default = max(psutil.cpu_count(logical = True), 1),
						help = "number of cores to use")
	parser.add_argument("--parallel_gpu", type = int, default = 1,
						help = "number of instance to run in parallel on the GPU, seems faster on some GPU if enough mem")

	parser.add_argument("--ffprobe", default = "ffprobe", help = "ffprobe command/path")
	parser.add_argument("--ffmpeg", default = "ffmpeg", help = "ffprobe command/path")
	return parser


if __name__ == "__main__":
	parser = make_parser()
	args = { }
	for name, value in vars(parser.parse_args()).items():
		args[name] = value

	pre_check()
	limit_resources(args)
	start(args)
