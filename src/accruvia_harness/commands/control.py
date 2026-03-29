from __future__ import annotations

from .common import CLIContext, emit


def handle_control_command(args, ctx: CLIContext) -> bool:
    control_plane = ctx.control_plane
    if args.command == "control-status":
        emit(control_plane.status())
        return True
    if args.command == "control-on":
        emit(control_plane.turn_on())
        return True
    if args.command == "control-off":
        emit(control_plane.turn_off())
        return True
    if args.command == "control-freeze":
        emit(control_plane.freeze(args.reason))
        return True
    if args.command == "control-thaw":
        emit(control_plane.thaw())
        return True
    if args.command == "control-pause-lane":
        emit(control_plane.pause_lane(args.lane_name, reason=args.reason))
        return True
    if args.command == "control-resume-lane":
        emit(control_plane.resume_lane(args.lane_name, reason=args.reason))
        return True
    return False
