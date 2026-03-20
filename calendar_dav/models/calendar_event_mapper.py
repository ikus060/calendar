# Copyright 2026 IKUS Software <patrik@ikus-soft.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import logging
import pytz
from datetime import datetime, date, timedelta
from odoo import models, Command
from odoo.addons.base_dav.models.dav_custom_mapper import DefaultMapper
import vobject
from odoo.tools import html2plaintext
from odoo.exceptions import AccessError
from collections import namedtuple

_logger = logging.getLogger(__name__)

PARTSTAT_TO_ODOO = {
    'ACCEPTED': 'accepted',
    'DECLINED': 'declined',
    'TENTATIVE': 'tentative',
    'NEEDS-ACTION': 'needsAction',
}

# Conversion factor from interval to minutes
UNIT_TO_MINUTES = {
    'minutes': 1,
    'hours': 60,
    'days': 60 * 24,
    'weeks': 60 * 24 * 7,
}

ODOO_TO_PARTSTAT = {v: k for k, v in PARTSTAT_TO_ODOO.items()}

OccurrenceException = namedtuple('OccurrenceException','original_start,outlier_event')

class CalendarEventMapper(DefaultMapper):
    """
    Converts between calendar.event records and calendar(VEVENT).
    Registered for dav_type=calendar in the collection registry.
    """
    _name = "dav.calendar.event.mapper"
    _description = "Calendar Event Mapper"
    _dav_model_id = "calendar.event"
    _dav_type = "calendar"
    _dav_field_uuid = "dav_uid"
    _dav_default_domain = ["|", "|", ("recurrence_id", "=", False), ("is_base_event", "=", True), ("follow_recurrence", "=", False)]

    #----------------------------------------------------------------------
    # Private helpers
    #----------------------------------------------------------------------

    def _datetime_to_db(self, dt):
        if dt.tzinfo:
            return dt.astimezone(pytz.utc).replace(tzinfo=None)
        return dt
    
    def _datetime_from_db(self, dt, event_tz=None):
        event_tz_name = (event_tz or self.env.user.tz or 'UTC')
        tzinfo = pytz.timezone(event_tz_name)
        return dt.replace(tzinfo=pytz.utc).astimezone(tzinfo)

    def _extract_event_tz(self, dt) -> str:
        """
        Extract timezone from a VEVENT datetime using icalendar's built-in timezone resolution.
        """
        
        tzinfo = getattr(dt, "tzinfo", None)
        if tzinfo is None:
            return None # UTC

        # icalendar builds a tzical tzinfo from VTIMEZONE blocks.
        # pytz.timezone() can resolve it via its zone attribute OR str()
        zone = getattr(tzinfo, "zone", None)  # pytz zones expose .zone directly
        if zone and zone in pytz.all_timezones_set:
            return zone

        # For tzicalvtz (built from VTIMEZONE), icalendar attaches the TZID
        if hasattr(tzinfo, '_tzid'):
            tzid = tzinfo._tzid
            try:
                return pytz.timezone(tzid).zone
            except pytz.exceptions.UnknownTimeZoneError:
                _logger.warning(
                    "CalDAV: Unknown timezone %r, falling back to UTC", zone
                )
        return None # UTC

    def _analyze_recurrence(self, record):
        """
        Analyze a recurrence to identify deleted occurrences (EXDATE)
        and modified occurrences (RECURRENCE-ID exceptions).

        The matching strategy:
        - Events whose start matches a theoretical start exactly to normal occurrences
        - Events whose start does NOT match any start to exceptions (modified)
        - Theoretical start with no matching event to EXDATE (deleted)

        Pairing exceptions to their original slots is done by generation order,
        since Odoo creates events sequentially from the recurrence expansion.

        Args:
            record: calendar.event recordset (single record, must be base_event).

        Returns:
            dict of dates and corresponding event. Value None if deleted or corresponding calendar.event.
        """
        # otherwise it's a RECURRENCE-ID.
        recurrence = record.recurrence_id
        if not recurrence or not recurrence.calendar_event_ids:
            return set(), {}

        # Step 1 : Compute theoretical slots from RRULE
        theoretical_starts = set(recurrence._get_occurrences(record.start))

        all_events = recurrence.calendar_event_ids.sorted(key=lambda e: e.start)

        # Step 2: Partition events
        # Normal: event.start matches a theoretical slot exactly
        # Outlier: event.start does NOT match any theoretical slot (was moved)
        normal_events = all_events.filtered(lambda e: e.start in theoretical_starts)
        outlier_events = all_events.filtered(lambda e: e.start not in theoretical_starts)

        normal_starts = set(normal_events.mapped('start'))

        # Step 3: Find vacant theoretical slots
        # Slots not covered by any normal event
        vacant_starts = set(theoretical_starts - normal_starts)

        result = {}
        if not outlier_events or not vacant_starts:
            # No outliers: all vacant slots are pure deletions
            return vacant_starts, {}

        # Step 4: Pair outlier events to vacant starts
        # Rationale: Match event to the closes vacant starts.
        #
        # This pairing is an approximation - Odoo does not persist the original slot.
        # Build candidate pairs sorted by ascending distance (greedy closest match)
        candidates = sorted(
            (
                (abs(event.start - start), event, start)
                for event in outlier_events
                for start in vacant_starts
            ),
            key=lambda x: x[0],
        )

        remaining_events = {e.id for e in outlier_events}

        for _distance, event, original_start in candidates:
            if event.id not in remaining_events:
                continue
            if original_start not in vacant_starts:
                continue
            # Associate start with matching event.
            result[event] = original_start
            remaining_events.discard(event.id)
            vacant_starts.discard(original_start)

            if not remaining_events or not vacant_starts:
                break

        return vacant_starts, result

    def _compute_modified_events(self, base_event):
        """
        For a recurrence, compute list of modified events
        """
        fields = ['name', 'description', 'location', 'videocall_location', 'partner_ids']

        def _modified(ev):
            return ev.id != base_event.id and any(getattr(ev, field) != getattr(base_event, field) for field in fields)

        return base_event.recurrence_id.calendar_event_ids.filtered(_modified)

    def _parse_attendees(self, record, attendee_list) -> tuple[list, list]:
        """
        Parse a list of ATTENDEE vobject properties into ORM commands
        for both partner_ids and attendee_ids fields, allowing a single write().

        Odoo's write() will call _attendees_values() when partner_ids is present,
        which would override attendee_ids. To avoid this, attendee_ids commands
        are built explicitly here to include the state, and partner_ids commands
        are used only to maintain the M2M relationship.

        Args:
            record: calendar.event recordset (single record).
            attendee_list (list): List of vobject attendee properties.

        Returns:
            tuple:
                - list: ORM commands for partner_ids field.
                - list: ORM commands for attendee_ids field (with state).
        """
        partner_commands = []
        attendee_commands = []

        # Index existing attendees by partner_id for quick lookup
        existing_attendees = { att.partner_id.id: att for att in record.attendee_ids } if record else {}

        for attendee_prop in attendee_list:
            email = attendee_prop.value.replace('mailto:', '').strip()

            if not email:
                continue

            partstat = attendee_prop.params.get('PARTSTAT', ['NEEDS-ACTION'])[0]
            odoo_state = PARTSTAT_TO_ODOO.get(partstat, 'needsAction')

            partner = self.env['res.partner'].search(
                [('email', '=ilike', email)], limit=1
            )
            if not partner:
                cn = attendee_prop.params.get('CN', [email])[0]
                partner = self.env['res.partner'].create(
                    {'name': cn, 'email': email}
                )

            partner_commands.append(Command.link(partner.id))

            existing_attendee = existing_attendees.get(partner.id)
            if existing_attendee:
                attendee_commands.append(Command.update(existing_attendee.id, {'state': odoo_state}))
            else:
                attendee_commands.append(Command.create({'partner_id': partner.id, 'state': odoo_state}))

        return partner_commands, attendee_commands

    def _get_or_create_category(self, name: str):
        """
        Return (or create) a res.partner.category record matching *name*.
        """
        tag = self.env["calendar.event.type"].search(
            [("name", "=", name)], limit=1
        )
        if not tag:
            try:
                tag = self.env["calendar.event.type"].create({"name": name})
            except AccessError:
                # User might not have the permissions.
                _logger.warning(
                    "CalDAV Mapper: could not create calendar tag %r - "
                    "insufficient rights. Tag will be skipped.",
                    name,
                )
                return self.env["calendar.event.type"]  # empty recordset
            tag = self.env["calendar.event.type"].create({"name": name})
        return tag


    def _apply_general(self, vevent, record):
        """Map summary, description, location and videocall fields."""
        values = {}

        # SUMMARY
        if hasattr(vevent, 'summary'):
            values['name'] = vevent.summary.value
        elif not record:
            # Odoo required a name
            values['name'] = '(No Title)'

        # DESCRIPTION (html or plain)
        if hasattr(vevent, 'x_alt_desc'):
            values['description'] = vevent.x_alt_desc.value
        elif hasattr(vevent, 'description'):
            values['description'] = vevent.description.value

        # LOCATION
        if hasattr(vevent, 'location'):
            values['location'] = vevent.location.value

        # URL / CONFERENCE
        if hasattr(vevent, 'url'):
            values['videocall_location'] = vevent.url.value
        elif hasattr(vevent, 'conference'):
            values['videocall_location'] = vevent.conference.value

        return values


    def _apply_dates(self, vevent, start=None):
        """Map dtstart/dtend to allday, start, stop fields."""
        values = {}

        if not hasattr(vevent, 'dtstart'):
            return values

        dtstart = start or vevent.dtstart.value

        if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
            values['allday'] = True
            values['start_date'] = dtstart

            if hasattr(vevent, 'dtend'):
                duration = vevent.dtend.value - vevent.dtstart.value
                values['stop_date'] = dtstart + duration
        else:
            values['allday'] = False
            values['start'] = self._datetime_to_db(dtstart)

            if hasattr(vevent, 'dtend'):
                duration = vevent.dtend.value - vevent.dtstart.value
                values['stop'] = self._datetime_to_db(dtstart) + duration

        return values

    def _apply_categories(self, vevent):
        """Map categories to categ_ids."""
        values = {}

        if not hasattr(vevent, 'categories'):
            return values

        tag_names = self.get_vobject_multi_value(vevent, "categories")
        categ_ids = [
            self._get_or_create_category(n.strip()).id
            for n in tag_names
            if n.strip()
        ]
        if categ_ids:
            values["categ_ids"] = [(6, 0, categ_ids)]

        return values


    def _apply_attendees(self, vevent, record):
        """
        Map attendees to partner_ids and attendee_ids.
        """
        values = {}

        # ATTENDEES
        if hasattr(vevent, 'attendee_list'):
            partner_commands, attendee_commands = self._parse_attendees(record, vevent.attendee_list)
            if partner_commands:
                values['partner_ids'] = partner_commands
                values['attendee_ids'] = attendee_commands
        elif not record:
            # Minimally, we need to have one attendee our-self
            values['partner_ids'] =  [Command.set([self.env.user.partner_id.id])]

        return values

    def _apply_alarms(self, vevent):
        """
        Map VALARM components to alarm_ids.

        Only DISPLAY (→ notification) and EMAIL alarms are supported.
        Alarms with positive triggers, absolute triggers, or RELATED=END
        are skipped. Existing calendar.alarm records with an equivalent
        total duration are reused; otherwise a new one is created using
        the unit as expressed in the VALARM.
        """
        values = {}

        valarms = [c for c in vevent.components() if c.name == "VALARM"]
        if not valarms:
            return values

        alarm_ids = []
        for valarm in valarms:
            # ACTION
            if not hasattr(valarm, 'action'):
                continue
            action = valarm.action.value.upper()
            if action == 'DISPLAY':
                alarm_type = 'notification'
            elif action == 'EMAIL':
                alarm_type = 'email'
            else:
                _logger.info("Skipping VALARM with unsupported ACTION: %s", action)
                continue

            # TRIGGER
            if not hasattr(valarm, 'trigger'):
                continue
            trigger_value = valarm.trigger.value

            if not isinstance(trigger_value, timedelta):
                _logger.info("Skipping VALARM with absolute TRIGGER: %s", trigger_value)
                continue

            related = valarm.trigger.params.get('RELATED', ['START'])
            if related and related[0].upper() == 'END':
                _logger.info("Skipping VALARM with RELATED=END trigger")
                continue

            total_seconds = trigger_value.total_seconds()
            if total_seconds > 0:
                _logger.info("Skipping VALARM with positive TRIGGER: %s", trigger_value)
                continue

            total_minutes = int(-total_seconds // 60)
            if total_minutes == 0:
                continue

            # Pick the unit as expressed in the VALARM (largest clean unit)
            if total_minutes % UNIT_TO_MINUTES['weeks'] == 0:
                interval = 'weeks'
            elif total_minutes % UNIT_TO_MINUTES['days'] == 0:
                interval = 'days'
            elif total_minutes % UNIT_TO_MINUTES['hours'] == 0:
                interval = 'hours'
            else:
                interval = 'minutes'
            duration = total_minutes // UNIT_TO_MINUTES[interval]

            # DESCRIPTION → name (only used at creation time)
            name = (
                valarm.description.value
                if hasattr(valarm, 'description') and valarm.description.value
                else _("Reminder")
            )

            alarm = self._get_or_create_alarm(
                alarm_type, duration, interval, total_minutes, name
            )
            alarm_ids.append(alarm.id)

        if alarm_ids:
            values["alarm_ids"] = [Command.set(alarm_ids)]

        return values

    def _get_or_create_alarm(self, alarm_type, duration, interval,
                            total_minutes, name):
        """Find an existing calendar.alarm with the same effective duration,
        regardless of the unit used to express it. Otherwise create one using
        the unit from the VALARM.
        """
        Alarm = self.env['calendar.alarm']

        alarm = Alarm.search([
            ('alarm_type', '=', alarm_type),
            ('duration_minutes', '=', total_minutes),
        ], limit=1)
        if alarm:
            return alarm

        return Alarm.create({
            'name': name,
            'alarm_type': alarm_type,
            'duration': duration,
            'interval': interval,
        })

    def _apply_rrule(self, vevent, record):
        """
        Map rrule to recurrency, rrule and event_tz fields.

        Handles scenarios:
        1. New recurring event (record is None)
        2. Existing non-recurring event transitioning to recurring
        3. Existing recurring event with rule change
        4. Existing recurring event transitioning to non-recurring
        5. No recurrence, no change needed
        """
        values = {}
        _rrule_parse = self.env["calendar.recurrence"]._rrule_parse

        if hasattr(vevent, "rrule"):
            new_rrule = vevent.rrule.value
            event_tz = self._extract_event_tz(vevent.dtstart.value)

            values["recurrency"] = True
            values["event_tz"] = event_tz

            # Scenario 1: New event - Odoo creates recurrence on write
            if not record:
                values["rrule"] = new_rrule

            # Scenario 2: Non-recurring to recurring
            elif not record.rrule:
                values["rrule"] = new_rrule
                # No recurrence_update needed: Odoo will create the recurrence

            # Scenario 3: Recurring to recurring with different rule
            elif new_rrule!=record.rrule and _rrule_parse(new_rrule,None) != _rrule_parse(record.rrule,None):
                values.update(_rrule_parse(new_rrule,None))
                values["recurrence_update"] = "all_events"

            # Scenario 3b: Same rule, no change needed — return empty
            # (avoid triggering unnecessary writes)

        else:
            # No RRULE in vevent

            # Scenario 4: Was recurring to now non-recurring
            if record and record.rrule:
                values["recurrency"] = False
                values["recurrence_id"] = False
                values["recurrence_update"] = "all_events"
                # FIXME Work partly - Odoo do not delete the recurrence_id
                # and also skip any field changes.

            # Scenario 5: Was never recurring to nothing to do
            # Return empty values, no write needed

        return values


    def _apply_exdates(self, vevent, record, collection):
        """
        Handle exclusion and restoration of recurrence occurrences.
        """
        is_base_vevent = not hasattr(vevent, 'recurrence_id')

        if not (is_base_vevent and (hasattr(vevent, "exdate") or record.recurrence_id)):
            return

        new_exdates = self.get_vobject_multi_value(vevent, "exdate")
        new_exdates = set(map(self._datetime_to_db, new_exdates))
        current_exdates, _outliers = self._analyze_recurrence(record)

        # Soft-delete occurrences newly added to EXDATE
        dates_to_exclude = [self._datetime_to_db(dt) for dt in (new_exdates - current_exdates)]
        if dates_to_exclude:
            self.env["calendar.event"].search([
                ("recurrence_id", "=", record.recurrence_id.id),
                ("start", "in", dates_to_exclude),
            ]).write({"active": False})

        # Restore occurrences removed from EXDATE
        dates_to_restore = [self._datetime_to_db(dt) for dt in (current_exdates - new_exdates)]
        if dates_to_restore:
            # First, let restore soft-deleted event.
            matching = self.env["calendar.event"].with_context(active_test=False).search([
                ("recurrence_id", "=", record.recurrence_id.id),
                ("start", "in", dates_to_restore),
            ])
            matching.write({"active": True})
            
            # Then create missing events.
            missing_dates = set(dates_to_restore) - {self._datetime_to_db(e.start) for e in matching}
            for missing_start in missing_dates:
                values = {}
                values.update(self._apply_general(vevent, None))
                values.update(self._apply_dates(vevent, start=missing_start))
                values.update(self._apply_categories(vevent))
                values.update(self._apply_attendees(vevent, None))
                values['recurrency'] = True
                values['recurrence_id'] = record.recurrence_id.id
                values['follow_recurrence'] = True
                # Custom mapping overrides on top
                values.update(self.extract_field_mapping_from_vobject(vevent, collection))
                # Create calendar event
                self.apply_values(None, values, collection)
    
    def _export_alarms(self, vevent, record):
        """
        Export alarms from a calendar.event record to a VEVENT vObject component.

        :param vevent: vobject VEVENT component to add VALARMs to
        :param record: calendar.event record
        """
        for alarm in record.alarm_ids:
            valarm = vevent.add('valarm')

            # ACTION
            if alarm.alarm_type == 'email':
                valarm.add('action').value = 'EMAIL'
            else:
                # 'notification' and any other type to DISPLAY
                valarm.add('action').value = 'DISPLAY'

            # DESCRIPTION (required for DISPLAY and EMAIL)
            valarm.add('description').value = alarm.name or 'Reminder'

            # TRIGGER — always negative (before event start), always relative to START
            interval = alarm.interval  # minutes, hours, days, weeks
            duration = alarm.duration

            if interval == 'minutes':
                delta = timedelta(minutes=-duration)
            elif interval == 'hours':
                delta = timedelta(hours=-duration)
            elif interval == 'days':
                delta = timedelta(days=-duration)
            elif interval == 'weeks':
                delta = timedelta(weeks=-duration)
            else:
                _logger.warning(
                    "calendar.alarm %s has unknown interval '%s', defaulting to minutes",
                    alarm.id, interval
                )
                delta = timedelta(minutes=-duration)

            valarm.add('trigger').value = delta

    def _export_general(self, vevent, record):
        """Export summary, description, location and videocall fields."""

        # SUMMARY
        if record.name:
            vevent.add('summary').value = record.name

        # DESCRIPTION
        if record.description:
            plain_text = html2plaintext(record.description)
            vevent.add('description').value = plain_text

            alt_desc = vevent.add('x-alt-desc')
            alt_desc.params['FMTTYPE'] = ['text/html']
            alt_desc.value = str(record.description)

        # LOCATION
        if record.location:
            vevent.add('location').value = record.location

        # URL / CONFERENCE
        if record.videocall_location:
            vevent.add('url').value = record.videocall_location
            conference = vevent.add('conference')
            conference.value = record.videocall_location


    def _export_dates(self, vevent, record):
        """Export dtstart/dtend from allday, start, stop fields."""

        if record.allday:
            start_date = record.start_date or record.start.date()
            stop_date = record.stop_date or record.stop.date()
            vevent.add('dtstart').value = start_date
            vevent.add('dtend').value = stop_date + timedelta(days=1)
        else:
            vevent.add('dtstart').value = self._datetime_from_db(record.start, record.event_tz)
            vevent.add('dtend').value = self._datetime_from_db(record.stop, record.event_tz)


    def _export_organizer(self, vevent, record):
        """Export organizer field. Returns org_email for use in attendees."""

        organizer = record.user_id.partner_id
        org_name = organizer.name or 'Unknown'
        org_email = (
            organizer.email
            or f'noreply+{organizer.id}@odoo.local'
        )

        organizer_prop = vevent.add('organizer')
        organizer_prop.value = f'mailto:{org_email}'
        organizer_prop.params['CN'] = [org_name]

        return org_email


    def _export_categories(self, vevent, record):
        """Export categ_ids as CATEGORIES."""

        if record.categ_ids:
            tag_names = record.categ_ids.mapped("name")
            vevent.add("categories").value = tag_names


    def _export_attendees(self, vevent, record, org_email):
        """Export attendee_ids as ATTENDEE properties."""

        for attendee in record.attendee_ids:
            email = (
                attendee.partner_id.email
                or f'noreply+{attendee.partner_id.id}@odoo.local'
            )
            name = attendee.partner_id.name or 'Unknown'

            attendee_prop = vevent.add('attendee')
            attendee_prop.value = f'mailto:{email}'
            if name:
                attendee_prop.params['CN'] = [name]

            if email == org_email:
                attendee_prop.params['ROLE'] = ['CHAIR']
                attendee_prop.params['PARTSTAT'] = ['ACCEPTED']
            else:
                attendee_prop.params['ROLE'] = ['REQ-PARTICIPANT']
                partstat_key = ODOO_TO_PARTSTAT.get(attendee.state, 'NEEDS-ACTION')
                attendee_prop.params['PARTSTAT'] = [partstat_key]

    def _extract_rrule_value(self, rrule_raw: str) -> str:
        """
        Extract only the RRULE value from a potentially full iCal recurrence string.
        
        Handles cases like:
        - "DTSTART:20260423T215020\nRRULE:FREQ=DAILY;COUNT=5"
        - "RRULE:FREQ=DAILY;COUNT=5"
        - "FREQ=DAILY;COUNT=5"
        
        Returns: "FREQ=DAILY;COUNT=5"
        """
        if not rrule_raw:
            return rrule_raw

        for line in rrule_raw.splitlines():
            line = line.strip()
            if line.upper().startswith("RRULE:"):
                return line[6:]  # Strip the "RRULE:" prefix

        # If no "RRULE:" line found, assume the value is already plain
        return rrule_raw.strip()

    def _export_recurrence(self, vevent, record, collection, cal, original_start):
        """Export rrule, exdates and recurrence-id for recurring events."""

        if not record.recurrency:
            return

        if record.is_base_event:
            # RRULE
            if record.rrule:
                vevent.add('rrule').value = self._extract_rrule_value(record.rrule)

            # EXDATE
            exdates, outliers = self._analyze_recurrence(record)
            if exdates:
                exdate_prop = vevent.add('exdate')
                exdate_prop.value = [
                    self._datetime_from_db(dt, record.event_tz)
                    for dt in sorted(exdates)
                ]

            # RECURRENCE-ID — outliers and modified instances
            modified_events = self._compute_modified_events(record)
            for outlier_event, original_start in outliers.items():
                self.to_vobject(outlier_event, collection, cal=cal, original_start=original_start)
            for child in modified_events:
                if child in outliers:
                    continue
                self.to_vobject(child, collection, cal=cal)

        else:
            # Child event — reference parent UID and original start
            vevent.add('UID').value = record.recurrence_id.base_event_id.dav_uid
            vevent.add('RECURRENCE-ID').value = self._datetime_from_db(
                original_start or record.start, record.event_tz
            )

    #----------------------------------------------------------------------
    # Public interface
    #----------------------------------------------------------------------

    def to_vobject(self, record, collection, cal=None, original_start=None):
        """
        Export a calendar.event record to a vobject VEVENT component.

        Args:
            record: calendar.event recordset (single record).
            collection: The collection definition.
            cal: Existing vobject VCALENDAR to append to, or None to create one.
            original_start: Original start datetime for modified recurrence instances.

        Returns:
            vobject VCALENDAR component.
        """
        # TODO should we call record.ensure_one()
        cal = cal or vobject.iCalendar()
        vevent = cal.add('vevent')

        self._export_general(vevent, record)
        self._export_dates(vevent, record)

        org_email = self._export_organizer(vevent, record)

        self._export_categories(vevent, record)
        self._export_attendees(vevent, record, org_email)
        self._export_alarms(vevent, record)
        self._export_recurrence(vevent, record, collection, cal, original_start)

        # Apply custom mapping
        self.apply_field_mapping_to_vobject(vevent, record, collection)

        return cal

    def from_vobject(self, vcal, record, collection):
        """
        Import/update a calendar.event record from a vobject VCALENDAR component.

        Args:
            vcal: A vobject VCALENDAR component.
            record: Existing calendar.event recordset, or empty recordset.
            collection: The collection definition.

        Returns:
            calendar.event record (created or updated).
        """
        # TODO Should we call record.ensure_one(). record should be empty, None or one.
        result = None
        for component in vcal.components():
            if component.name == 'VTIMEZONE':
                continue
            if component.name != "VEVENT":
                raise ValueError("Not a VEVENT component.")
            vevent = component
            
            # To handle recurrence, check which record need to be updated, the base event or a single occurence.
            is_base_vevent = not hasattr(vevent, 'recurrence_id')
            if is_base_vevent:
                record_to_update = record
            elif isinstance(vevent.recurrence_id.value, datetime):
                record_to_update = self.env["calendar.event"].with_context(active_test=False).search([
                    ("recurrence_id", "=", record.recurrence_id.id),
                    ("start", "=", self._datetime_to_db(vevent.recurrence_id.value)),
                ])
            elif isinstance(vevent.recurrence_id.value, date):
                # Recurrence for all-day event
                record_to_update = self.env["calendar.event"].with_context(active_test=False).search([
                    ("recurrence_id", "=", record.recurrence_id.id),
                    ("start_date", "=", vevent.recurrence_id.value),
                ])
            else:
                raise ValueError(f'invalid recurrence_id: {vevent.recurrence_id.value}')

            values = {}
            values.update(self._apply_general(vevent, record))
            values.update(self._apply_dates(vevent))
            values.update(self._apply_categories(vevent))
            values.update(self._apply_alarms(vevent))
            values.update(self._apply_attendees(vevent, record_to_update))
            if is_base_vevent:
                values.update(self._apply_rrule(vevent, record_to_update))

            # Custom mapping overrides on top
            values.update(self.extract_field_mapping_from_vobject(vevent, collection))

            # Update or create calendar event
            result = self.apply_values(record_to_update, values, collection)

            # Handle exdates post-save
            if is_base_vevent:
                self._apply_exdates(vevent, result, collection)

        return result
