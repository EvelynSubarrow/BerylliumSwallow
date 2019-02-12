#!/usr/bin/env python3

import datetime, sys
from collections import OrderedDict

import psycopg2, psycopg2.extras
import curses

from common import database

def process_time(unix_time):
    out = OrderedDict([("ut",None), ("iso", None), ("date", None), ("short", "")])
    if unix_time:
        dt = datetime.datetime.fromtimestamp(float(unix_time))
        out["ut"] = unix_time
        out["iso"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
        out["date"] = dt.strftime("%Y-%m-%d")
        out["short"] = dt.strftime("%H%M") + "½"*(dt.second==30)
    return out

# Python justification is /good/ but it's not quite perfect for this
def platform_justify(str, length):
    # If this happens, something's gone horribly wrong
    if length!=3: raise ValueError()
    # "   "
    if not str:
        return "   "
    # " 0 "
    elif len(str)==1:
        return " {} ".format(str)
    # " 3R" / "12A"
    elif str[-1].isalpha() and str[0].isnumeric():
        return str.rjust(3)
    # "12 " / "LH "
    else:
        return str.ljust(3)

class Data():
    def __init__(self, schema, data):
        self.schema = schema
        self.data = data

class Buffer():
    def __init__(self):
        self.data = []
        self.format = []
        self.title = ""
        self.line_offset = 0
        self.lines = []
        self.col_names = []
        self.last_refreshed = datetime.datetime.now()

    def substitute_fn(self, x,y,z):
        return z

    def renew(self):
        self.col_names.clear()
        self.lines.clear()

        # Column headers
        for column in self.format:
            inherited_name, inherited_pad, inherited_justify, inherited_color = (None,)*4
            current_schema = self.data.schema
            for level in column.split("/"):
                inherited_name, inherited_pad, inherited_justify, inherited_color = [a or b for a,b in zip((inherited_name, inherited_pad, inherited_justify, inherited_color), (current_schema.get("_",()) + (None,None,None, None))[:4] )]
                current_schema = current_schema[level]
            name, pad, color_scheme = current_schema[0], current_schema[1], 0

            if len(current_schema)>=4:
                color_scheme = current_schema[3]

            name, pad, color_scheme = name or inherited_name, pad or inherited_pad, color_scheme or inherited_color or 0

            self.col_names.append((name[:pad].center(pad), 0))

        # Columns themselves
        for row in self.data.data:
            line = []
            for display_col in self.format:
                inherited_name, inherited_pad, inherited_justify, inherited_color = (None,)*4
                current_cell = row
                current_schema = self.data.schema
                for level in display_col.split("/"):
                    inherited_name, inherited_pad, inherited_justify, inherited_color = [a or b for a,b in zip((inherited_name, inherited_pad, inherited_justify, inherited_color), (current_schema.get("_",()) + (None,None,None,None))[:4] )]
                    current_cell = current_cell[level]
                    current_schema = current_schema[level]

                pad, justify, color_scheme = current_schema[1], None, 0
                if len(current_schema)>=3:
                    justify = current_schema[2]
                if len(current_schema)>=4:
                    color_scheme = current_schema[3]

                pad, justify, color_scheme = pad or inherited_pad, justify or inherited_justify, color_scheme or inherited_color or 0

                final_row_text, final_row_color = self.substitute_fn(row, display_col, (str(current_cell), color_scheme))

                if current_cell==None:
                    final_row_text = ""
                final_row_text = (justify or str.ljust)(final_row_text, pad)

                line.append((final_row_text, final_row_color))

            self.lines.append(line)
        self.invalidate()

    def scroll_up(self,n):
        self.line_offset = max(self.line_offset-n, 0)
        self.invalidate()

    def scroll_down(self,n):
        self.line_offset = min(self.line_offset+n, len(self.lines))
        self.invalidate()

    def position_summary(self,dim_lines):
        return "[{}..{}/{}]".format(self.line_offset+1, min(self.line_offset+dim_lines, len(self.lines)), len(self.lines))

    def render(self, window, dim_lines, dim_cols):
        for i,row in enumerate(self.lines[self.line_offset:self.line_offset+dim_lines]):
            next_col_x = 0
            for column,attr in row:
                # Intersperse odd columns with dots to make it easier to read across
                if next_col_x and i%2:
                    window.hline(i, next_col_x-1, curses.ACS_BULLET, 1)
                window.addstr(i, next_col_x, column, curses.color_pair(attr))
                next_col_x += len(column) + 1
        if self.body_outstanding:
            window.refresh()
        self.body_outstanding = False

    def render_headers(self, dim_lines, dim_cols, title_window, cols_window):
        title_window.addstr(0, 0, self.title, curses.A_BOLD)
        current_view_str = self.position_summary(dim_lines)
        title_window.addstr(0, dim_cols-len(current_view_str)-1, current_view_str, curses.A_BOLD)

        next_col_x = 0
        for column,attr in self.col_names:
            if next_col_x:
                cols_window.hline(0, next_col_x-1, curses.ACS_VLINE, 1)

            cols_window.addstr(0, next_col_x, column, curses.color_pair(attr))
            next_col_x += len(column) + 1

        if self.headers_outstanding:
            title_window.refresh()
            cols_window.refresh()
        self.headers_outstanding = False

    def invalidate(self):
        self.headers_outstanding, self.body_outstanding = True, True

    def refresh(self):
        self.invalidate()

    def consider_refresh(self):
        if (datetime.datetime.now()-self.last_refreshed).seconds > 30:
            self.last_refreshed = datetime.datetime.now()
            self.refresh()

class TextBuffer(Buffer):
    def __init__(self, title, data, format):
        super(TextBuffer, self).__init__()
        self.data = data
        self.format = format
        self.title = title
        self.renew()

class BoardBuffer(Buffer):
    def __init__(self, dt_start, duration, location_code):
        super(BoardBuffer, self).__init__()
        self.dt_start, self.duration, self.location_code = dt_start, duration, location_code
        self.format = ["service/uid", "service/atoc_code", "service/category",
            "service/power_type", "service/operating_characteristics", "service/signalling_id",
            "arrival_scheduled/short", "departure_scheduled/short", "pass_scheduled/short", "platform", "service/current_variation",
            "arrival_actual/short", "departure_actual/short", "origin/name", "destination/name"
            ]
        self.refresh()

    def refresh(self):
        self.title = "STATION DEPARTURE BOARD ENQUIRY - {} {:%Y-%m-%d %H:%M:%S} - {} MINUTES".format(self.location_code, self.dt_start, self.duration)
        self.data = self.get_board(self.dt_start, self.duration, self.location_code)
        self.renew()

    def substitute_fn(self, x,y,z):
        # De-emphasise station matching query
        if y.startswith("origin") and x["here"]["tiploc"]==x["origin"]["tiploc"]:
            return (z[0], 5)
        if y.startswith("destination") and x["here"]["tiploc"]==x["destination"]["tiploc"]:
            return (z[0], 5)
        # Only display general estimate if the train hasn't yet been, and nothing if it's off route
        if y=="service/current_variation" and (x["arrival_actual"]["ut"] or x["departure_actual"]["ut"]):
            movement = x["trust_departure"]
            if not movement["variation_status"]:
                movement = x["trust_arrival"]
            if movement["variation_status"] in "EL":
                return ("{variation}{variation_status}".format(**movement), 5)
            else:
                # Off route
                return ("", 5)
        # Fill in columns with live running data
        if y=="service/signalling_id" and x["service"]["actual_signalling_id"]:
            return (x["service"]["actual_signalling_id"], 3)
        if y=="platform" and (x["trust_arrival"]["platform"] or x["trust_departure"]["platform"]):
            return ((x["trust_departure"]["platform"] or x["trust_arrival"]["platform"], 3))
        # Emphasise a characteristic containing Q ("runs as required")
        if y=="service/operating_characteristics" and "Q" in x["service"]["operating_characteristics"]:
            return (z[0], 7)
        return z

    def get_board(self, starting_datetime, duration, location):
        ret = []

        time_schema_short = {
            "iso":    (None, 19),
            "short":  (None, 4),
        }
        time_schema = {
            "iso":    (None, 19),
            "short":  (None, 5),
        }

        location_schema = {
            "name":   (None, 30),
            "tiploc": (None, 7),
            "crs":    (None, 3),
        }

        schema = {
            "service": {
                "uid":                  ("UID", 6, str.rjust),
                "atoc_code":            ("o.", 2),
                "power_type":           ("pw.", 3),
                "category":             ("c.", 2),
                "signalling_id":        ("sig.", 4),
                "actual_signalling_id": ("rt.s", 4, None, 3),
                "current_variation":    ("v.", 3, str.rjust, 3),
                "operating_characteristics": ("oc.", 4),
            },
            "trust_arrival": {
                "platform": ("pt.", 3, platform_justify),
            },

            "trust_departure": {
                "platform": ("pt.", 3, platform_justify),
            },

            "platform": ("pt.", 3, platform_justify),

            "arrival_scheduled":   {"_": ("arrival",),   **time_schema},
            "departure_scheduled": {"_": ("departure",), **time_schema},
            "pass_scheduled":      {"_": ("pass",),      **time_schema},

            "arrival_actual":   {"_": ("arrival",  None, None, 3), **time_schema_short},
            "departure_actual": {"_": ("departure",None, None, 3), **time_schema_short},

            "origin":      {"_": ("origin",),      **location_schema},
            "destination": {"_": ("destination",), **location_schema},
        }

        timestamp = int(starting_datetime.timestamp())
        with database.DatabaseConnection() as db_connection, db_connection.new_cursor() as c:
            c.execute("""SELECT
                arrival_scheduled,departure_scheduled,pass_scheduled,
                ta.datetime_actual,td.datetime_actual,
                arrival_public, departure_public,
                platform, line, path, activity, engineering_allowance, pathing_allowance, performance_allowance,
                ta.actual_platform, ta.actual_line, ta.actual_route, ta.actual_variation_status, ta.actual_variation, ta.actual_direction, ta.actual_source,
                td.actual_platform, td.actual_line, td.actual_route, td.actual_variation_status, td.actual_variation, td.actual_direction, td.actual_source,

                flat_schedules.uid, category, signalling_id, headcode, power_type, timing_load, speed, operating_characteristics, seating_class, sleepers,
                reservations, catering, branding, uic_code, atoc_code,

                start_date, actual_signalling_id, trust_id, current_variation,

                l0.tiploc, l0.name, l0.stanox, l0.crs,
                l1.tiploc, l1.name, l1.stanox, l1.crs,
                l2.tiploc, l2.name, l2.stanox, l2.crs,
                l3.tiploc, l3.name, l3.stanox, l3.crs,
                l4.tiploc, l4.name, l4.stanox, l4.crs

                FROM flat_timing
                INNER JOIN schedule_locations ON schedule_location_iid=schedule_locations.iid
                INNER JOIN schedules ON schedule_locations.schedule_iid=schedules.iid
                INNER JOIN flat_schedules ON flat_timing.flat_schedule_iid=flat_schedules.iid

                INNER JOIN locations as l0 ON schedule_locations.location_iid=l0.iid
                INNER JOIN locations as l1 ON schedules.origin_location_iid=l1.iid
                INNER JOIN locations as l2 ON schedules.destination_location_iid=l2.iid
                LEFT JOIN locations as l3 ON flat_schedules.current_location=l3.iid
                LEFT JOIN locations as l4 ON flat_schedules.cancellation_location=l4.iid

                LEFT JOIN trust_movements as ta ON
                (ta.movement_type='A' AND flat_schedules.iid=ta.flat_schedule_iid AND arrival_scheduled=ta.datetime_scheduled)
                LEFT JOIN trust_movements as td ON
                (td.movement_type='D' AND flat_schedules.iid=td.flat_schedule_iid AND (departure_scheduled=td.datetime_scheduled OR pass_scheduled=td.datetime_scheduled))
                WHERE flat_timing.location_iid=(select iid from locations where crs=%s)
                AND departure_scheduled BETWEEN %s AND %s ORDER BY departure_scheduled;
                """, [location, timestamp, timestamp+60*duration])

            for row in c:
                row = list(row)
                out = OrderedDict()
                for tag in ["arrival_scheduled", "departure_scheduled", "pass_scheduled", "arrival_actual", "departure_actual"]:
                    out[tag] = process_time(row.pop(0))

                for tag in ["arrival_public", "departure_public", "platform", "line", "path", "activity", "engineering_allowance", "pathing_allowance", "performance_allowance"]:
                    out[tag] = row.pop(0)

                out["trust_arrival"] = OrderedDict([(tag,row.pop(0)) for tag in ["platform", "line", "route", "variation_status", "variation", "direction", "source"]])
                out["trust_departure"] = OrderedDict([(tag,row.pop(0)) for tag in ["platform", "line", "route", "variation_status", "variation", "direction", "source"]])

                out["service"], out["here"], out["origin"], out["destination"], out["last_location"], out["cancellation_location"] = OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict()

                for tag in ["uid", "category", "signalling_id", "headcode", "power_type", "timing_load", "speed", "operating_characteristics", "seating_class", "sleepers", "reservations", "catering", "branding", "uic_code", "atoc_code", "date", "actual_signalling_id", "trust_id", "current_variation"]:
                    out["service"][tag] = row.pop(0)

                out["service"]["operating_characteristics"] = out["service"]["operating_characteristics"].rstrip()
                if out["platform"]:
                    out["platform"] = out["platform"].rstrip()

                for first in ["here", "origin", "destination", "last_location", "cancellation_location"]:
                    for second in ["tiploc", "name", "stanox", "crs"]:
                        out[first][second] = row.pop(0)

                ret.append(out)
            return Data(schema, ret)

class ServiceBuffer(Buffer):
    def __init__(self, date_start, service_code):
        super(ServiceBuffer, self).__init__()
        self.date_start, self.service_code = date_start, service_code
        self.format = ["activity", "arrival_scheduled/short", "departure_scheduled/short",
            "pass_scheduled/short", "service/current_variation", "arrival_actual/short", "trust_arrival/source",
            "departure_actual/short", "trust_departure/source", "platform", "here/tiploc", "here/name"
            ]
        self.refresh()

    def refresh(self):
        self.title = "SERVICE ENQUIRY - {} on {:%Y-%m-%d}".format(self.service_code, self.date_start)
        self.data = self.get_board(self.date_start, self.service_code)
        self.renew()

    def substitute_fn(self, x,y,z):
        # Only display general estimate if the train hasn't yet been, and nothing if it's off route
        if y=="service/current_variation" and (x["arrival_actual"]["ut"] or x["departure_actual"]["ut"]):
            movement = x["trust_departure"]
            if not movement["variation_status"]:
                movement = x["trust_arrival"]
            if movement["variation_status"] in "EL":
                return ("{variation}{variation_status}".format(**movement), 5)
            else:
                # Off route
                return ("", 5)
        elif y=="service/current_variation":
            return ("", 0)
        # Fill in columns with live running data
        if y=="platform" and (x["trust_arrival"]["platform"] or x["trust_departure"]["platform"]):
            return ((x["trust_departure"]["platform"] or x["trust_arrival"]["platform"], 3))
        return z

    def get_board(self, start_date, service_code):
        ret = []

        time_schema_short = {
            "iso":    (None, 19),
            "short":  (None, 4),
        }
        time_schema = {
            "iso":    (None, 19),
            "short":  (None, 5),
        }

        location_schema = {
            "name":   (None, 30),
            "tiploc": (None, 7),
            "crs":    (None, 3),
        }

        schema = {
            "service": {
                "uid":                  ("UID", 6, str.rjust),
                "atoc_code":            ("o.", 2),
                "power_type":           ("pw.", 3),
                "category":             ("c.", 2),
                "signalling_id":        ("sig.", 4),
                "actual_signalling_id": ("rt.s", 4, None, 3),
                "current_variation":    ("v.", 3, str.rjust, 3),
                "operating_characteristics": ("oc.", 4),
            },
            "trust_arrival": {
                "platform": ("pt.", 3, platform_justify),
                "source": ("source", 1),
            },

            "trust_departure": {
                "platform": ("pt.", 3, platform_justify),
                "source": ("source", 1)
            },

            "platform": ("pt.", 3, platform_justify),
            "activity": ("activity", 12),

            "arrival_scheduled":   {"_": ("arrival",),   **time_schema},
            "departure_scheduled": {"_": ("departure",), **time_schema},
            "pass_scheduled":      {"_": ("pass",),      **time_schema},

            "arrival_actual":   {"_": ("arrival",  None, None, 3), **time_schema_short},
            "departure_actual": {"_": ("departure",None, None, 3), **time_schema_short},

            "here":      {"_": ("location",),      **location_schema},
            "origin":      {"_": ("origin",),      **location_schema},
            "destination": {"_": ("destination",), **location_schema},
        }

        with database.DatabaseConnection() as db_connection, db_connection.new_cursor() as c:
            c.execute("""SELECT
                arrival_scheduled,departure_scheduled,pass_scheduled,
                ta.datetime_actual,td.datetime_actual,
                arrival_public, departure_public,
                platform, line, path, activity, engineering_allowance, pathing_allowance, performance_allowance,
                ta.actual_platform, ta.actual_line, ta.actual_route, ta.actual_variation_status, ta.actual_variation, ta.actual_direction, ta.actual_source,
                td.actual_platform, td.actual_line, td.actual_route, td.actual_variation_status, td.actual_variation, td.actual_direction, td.actual_source,

                flat_schedules.uid, category, signalling_id, headcode, power_type, timing_load, speed, operating_characteristics, seating_class, sleepers,
                reservations, catering, branding, uic_code, atoc_code,

                start_date, actual_signalling_id, trust_id, current_variation,

                l0.tiploc, l0.name, l0.stanox, l0.crs,
                l1.tiploc, l1.name, l1.stanox, l1.crs,
                l2.tiploc, l2.name, l2.stanox, l2.crs,
                l3.tiploc, l3.name, l3.stanox, l3.crs,
                l4.tiploc, l4.name, l4.stanox, l4.crs

                FROM flat_timing
                INNER JOIN schedule_locations ON schedule_location_iid=schedule_locations.iid
                INNER JOIN schedules ON schedule_locations.schedule_iid=schedules.iid
                INNER JOIN flat_schedules ON flat_timing.flat_schedule_iid=flat_schedules.iid

                INNER JOIN locations as l0 ON schedule_locations.location_iid=l0.iid
                INNER JOIN locations as l1 ON schedules.origin_location_iid=l1.iid
                INNER JOIN locations as l2 ON schedules.destination_location_iid=l2.iid

                LEFT JOIN locations as l3 ON flat_schedules.current_location=l3.iid
                LEFT JOIN locations as l4 ON flat_schedules.cancellation_location=l4.iid

                LEFT JOIN trust_movements as ta ON
                (ta.stanox=l0.stanox AND ta.movement_type='A' AND flat_schedules.iid=ta.flat_schedule_iid AND arrival_scheduled=ta.datetime_scheduled)
                LEFT JOIN trust_movements as td ON
                (td.stanox=l0.stanox AND td.movement_type='D' AND flat_schedules.iid=td.flat_schedule_iid AND (departure_scheduled=td.datetime_scheduled OR pass_scheduled=td.datetime_scheduled))
                WHERE flat_schedules.uid=%s AND flat_schedules.start_date=%s
                ORDER BY flat_timing.schedule_location_iid;
                """, [service_code, start_date])

            for row in c:
                row = list(row)
                out = OrderedDict()
                for tag in ["arrival_scheduled", "departure_scheduled", "pass_scheduled", "arrival_actual", "departure_actual"]:
                    out[tag] = process_time(row.pop(0))

                for tag in ["arrival_public", "departure_public", "platform", "line", "path", "activity", "engineering_allowance", "pathing_allowance", "performance_allowance"]:
                    out[tag] = row.pop(0)

                out["trust_arrival"] = OrderedDict([(tag,row.pop(0)) for tag in ["platform", "line", "route", "variation_status", "variation", "direction", "source"]])
                out["trust_departure"] = OrderedDict([(tag,row.pop(0)) for tag in ["platform", "line", "route", "variation_status", "variation", "direction", "source"]])

                out["service"], out["here"], out["origin"], out["destination"], out["last_location"], out["cancellation_location"] = OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict()

                for tag in ["uid", "category", "signalling_id", "headcode", "power_type", "timing_load", "speed", "operating_characteristics", "seating_class", "sleepers", "reservations", "catering", "branding", "uic_code", "atoc_code", "date", "actual_signalling_id", "trust_id", "current_variation"]:
                    out["service"][tag] = row.pop(0)

                out["service"]["operating_characteristics"] = out["service"]["operating_characteristics"].rstrip()
                if out["platform"]:
                    out["platform"] = out["platform"].rstrip()

                for first in ["here", "origin", "destination", "last_location", "cancellation_location"]:
                    for second in ["tiploc", "name", "stanox", "crs"]:
                        out[first][second] = row.pop(0)

                ret.append(out)
            return Data(schema, ret)

def main(stdscr):
    curses.use_default_colors()
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(1)
    # Force getch to be non-blocking, 10=1s
    curses.halfdelay(10)
    # Allow mouse input
    curses.mousemask (1)

    # This sorcery is necessary for no readily appreciable reason
    stdscr.refresh()

    curses.init_pair(1, 0x0F, 0x5C) # White on purple
    curses.init_pair(2, 0x0F, 0xEE) # White on grey
    curses.init_pair(4, -1,   0xEA) # Default on dark grey (to visually separate the scheduled timing)

    curses.init_pair(3, 0x0E, -1)   # Aqua on default (used to indicate live data)
    curses.init_pair(5, 0xF8,  -1)  # Light grey on default (body de-emphasis)
    curses.init_pair(6, 0x0F,  -1)  # Bright white on default (body main)
    curses.init_pair(7, 0x0B,  -1)  # Bright yellow on default (body characteristic emphasis)

    window_header = curses.newwin(1,curses.COLS, 0,0)
    window_header.bkgd(" ", curses.color_pair(1))

    window_subheader = curses.newwin(1,curses.COLS, 1,0)
    window_subheader.bkgd(" ", curses.color_pair(2))

    window_footer = curses.newwin(1,curses.COLS, curses.LINES-1,0)
    window_footer.bkgd(" ", curses.color_pair(2))

    window_body = curses.newwin(curses.LINES-3,curses.COLS, 2,0)
    window_body.bkgd(" ", curses.color_pair(6))

    compose = ""
    cursor_pos = 0
    k = ""
    text_entry_mode = False

    current_buffer = TextBuffer(
        "Welcome to BeryilliumSwallow",
        Data({"body": ("Body", 4)}, [{"body": "line {}".format(i)} for i in range(1,80)]),
        ["body"],
        )

    while True:
        win_lines, win_cols = stdscr.getmaxyx()
        window_body_height = win_lines-2+text_entry_mode # This is a synonym for number of entries displayed

        # If it's been long enough since the last refresh, new data will be pulled in!
        current_buffer.consider_refresh()

        curses.curs_set(0) # No cursor flicker pls

        # Header with query title
        window_header.clear()
        window_header.resize(1, win_cols)
        window_subheader.clear()
        window_subheader.resize(1, win_cols)

        current_buffer.render_headers(window_body_height, win_cols, window_header, window_subheader)

        # Window for individual entries
        window_body.clear()
        window_body.resize(window_body_height, win_cols)

        current_buffer.render(window_body, window_body_height, win_cols)

        # Footer with composed command input
        window_footer.clear()
        if text_entry_mode:
            window_footer.mvwin(win_lines-1, 0)

            window_footer.addstr(0,0, compose)

            window_footer.move(0, cursor_pos)
            window_footer.refresh()

            # Now the cursor's in position, display it
            curses.curs_set(1)

        k = stdscr.getch()
        if k == curses.KEY_RESIZE:
            win_lines, win_cols = stdscr.getmaxyx()
            current_buffer.invalidate()

        if text_entry_mode:
            if k == curses.KEY_BACKSPACE:
                if cursor_pos:
                    compose = compose[:cursor_pos-1] + compose[cursor_pos:]
                    cursor_pos -= 1
            elif k == 0x14A:
                if cursor_pos:
                    compose = compose[:cursor_pos] + compose[cursor_pos+1:]
            elif k == curses.KEY_LEFT:
                cursor_pos = max(cursor_pos-1, 0)
            elif k == curses.KEY_RIGHT:
                cursor_pos = min(cursor_pos+1, len(compose))
            #elif k == 0x17: # ^W
            #    compose = compose[:compose.rfind(" ", 0, cursor_pos)] + compose[cursor_pos:]
            #    cursor_pos = len(compose)
            elif k == curses.KEY_ENTER or k == 0x0A:
                if compose.lower().startswith("trjd "):
                    crs = compose.split(" ")[1].upper()
                    dt_now = datetime.datetime.now() - datetime.timedelta(minutes=10)
                    current_buffer = BoardBuffer(dt_now, 120, crs)
                elif compose.lower().startswith("uid "):
                    uid = compose.split(" ")[1].upper()
                    start_date = datetime.datetime.strptime(compose.split(" ")[2], "%Y-%m-%d").date()
                    current_buffer = ServiceBuffer(start_date, uid)

                compose = ""
                cursor_pos = 0
                text_entry_mode = False
            elif 0x20 <= k <= 0x7E: # ' '..'~'
                if cursor_pos < win_cols-1:
                    compose = compose[:cursor_pos] + chr(k) + compose[cursor_pos:]
                    cursor_pos += 1
                else:
                    curses.beep()
        else:
            if k == ord("t") or k == ord(":"):
                text_entry_mode = True
            elif k == ord("q"):
                break
            elif k == curses.KEY_DOWN:
                current_buffer.scroll_down(1)
            elif k == curses.KEY_UP:
                current_buffer.scroll_up(1)

curses.wrapper(main)
