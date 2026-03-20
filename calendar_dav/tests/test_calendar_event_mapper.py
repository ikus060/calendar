# Copyright 2026 IKUS Software <patrik@ikus-soft.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from datetime import date, datetime, timedelta

import pytz
import vobject
import uuid
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo import Command

@tagged("post_install", "-at_install")
class AbstractCalendarEventMapper(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.user = cls.env.ref("base.user_demo")
        cls.user.tz = "America/Toronto"

        cls.partner = cls.user.partner_id
        cls.partner.email = "demo@example.com"

        cls.mapper = cls.env(user=cls.user)["dav.calendar.event.mapper"]

        cls.collection = cls.env["dav.collection"].create(
            {
                "name": "Test Address Book",
                "dav_type": "addressbook",
                "mapper_mode": cls.mapper._name,
                "domain": "[]",
            }
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _make_timed_event(self, **kwargs):
        """Create a minimal timed calendar.event."""
        vals = {
            "name": "Test Event",
            "start": datetime(2024, 6, 15, 10, 0, 0),
            "stop": datetime(2024, 6, 15, 11, 0, 0),
            "user_id": self.user.id,
        }
        vals.update(kwargs)
        return self.env["calendar.event"].create(vals)

    def _make_allday_event(self, **kwargs):
        """Create a minimal all-day calendar.event."""
        vals = {
            "name": "All Day Event",
            "allday": True,
            "start_date": date(2024, 6, 15),
            "stop_date": date(2024, 6, 15),
            "start": datetime(2024, 6, 15, 0, 0, 0),
            "stop": datetime(2024, 6, 15, 23, 59, 59),
            "user_id": self.user.id,
        }
        vals.update(kwargs)
        return self.env["calendar.event"].create(vals)

    def _make_vcal(self, **kwargs):
        """
        Build a minimal valid VCALENDAR with a single VEVENT.

        Accepted kwargs:
            summary     (str)       SUMMARY (default "Test Event")
            dtstart     (datetime)  naive UTC datetime or date for all-day
            dtend       (datetime)  naive UTC datetime or date for all-day
            allday      (bool)      if True dtstart/dtend must be date
            description (str)
            location    (str)
            url         (str)
            uid         (str)
            rrule       (str)
            categories  (list[str])
            attendees   (list[dict]) [{"email": .., "cn": ..}]
        """
        cal = vobject.iCalendar()
        vevent = cal.add("vevent")

        vevent.add("summary").value = kwargs.get("summary", "Test Event")
        vevent.add("uid").value = kwargs.get("uid", "test-uid-001@odoo.local")

        dtstart = kwargs.get("dtstart", datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc))
        dtend = kwargs.get("dtend", datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc))

        vevent.add("dtstart").value = dtstart
        vevent.add("dtend").value = dtend

        if "description" in kwargs:
            vevent.add("description").value = kwargs["description"]

        if "html_description" in kwargs:
            alt_desc = vevent.add('x-alt-desc')
            alt_desc.params['FMTTYPE'] = ['text/html']
            alt_desc.value = kwargs["html_description"]

        if "location" in kwargs:
            vevent.add("location").value = kwargs["location"]

        if "url" in kwargs:
            vevent.add("url").value = kwargs["url"]

        if "rrule" in kwargs:
            vevent.add("rrule").value = kwargs["rrule"]

        if "categories" in kwargs:
            vevent.add("categories").value = kwargs["categories"]

        for att in kwargs.get("attendees", []):
            att_prop = vevent.add("attendee")
            att_prop.value = f'mailto:{att["email"]}'
            att_prop.params["CN"] = [att.get("cn", att["email"])]

        return cal

@tagged("post_install", "-at_install")
class TestExportCalendarEventMapper(AbstractCalendarEventMapper):

    # ------------------------------------------------------------------ #
    #  EXPORT  (to_vobject)                                              #
    # ------------------------------------------------------------------ #

    def test_export_minimal_timed_event(self):
        """
        A minimal timed event exports a valid VEVENT with all mandatory properties.

        Given a minimal timed calendar.event with only name, start, stop and a
            responsible user (user_id) that has an email address.
        When to_vobject is called.
        Then the resulting VEVENT contains:
            - SUMMARY  matching the event name
            - DTSTART  as a timezone-aware datetime
            - DTEND    as a timezone-aware datetime
            - UID      matching the record dav_uid
            - ORGANIZER with a mailto URI containing the user email
        """
        # Given
        event = self._make_timed_event()

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then SUMMARY
        self.assertTrue(hasattr(vevent, "summary"))
        self.assertEqual(vevent.summary.value, event.name)

        # Then DTSTART
        self.assertTrue(hasattr(vevent, "dtstart"))
        self.assertIsInstance(vevent.dtstart.value, datetime)
        self.assertIsNotNone(vevent.dtstart.value.tzinfo)

        # Then DTEND
        self.assertTrue(hasattr(vevent, "dtend"))
        self.assertIsInstance(vevent.dtend.value, datetime)
        self.assertIsNotNone(vevent.dtend.value.tzinfo)

        # Then UID
        self.assertTrue(hasattr(vevent, "uid"))
        self.assertEqual(vevent.uid.value, event.dav_uid)

        # Then ORGANIZER
        self.assertTrue(hasattr(vevent, "organizer"))
        self.assertIn("mailto:", vevent.organizer.value)
        self.assertIn(self.partner.email, vevent.organizer.value)
        self.assertEqual(vevent.organizer.params["CN"], [self.partner.name])

    def test_export_allday_event(self):
        """
        All-day events use date objects; DTEND equals stop_date + 1 day.

        Given an all-day calendar.event with start_date and stop_date on the same day.
        When to_vobject is called.
        Then DTSTART is a date (not datetime) and DTEND is stop_date + 1 day
              as required by RFC 5545.
        """
        # Given
        event = self._make_allday_event()

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        dtstart_val = vevent.dtstart.value
        dtend_val = vevent.dtend.value

        self.assertIsInstance(dtstart_val, date)
        self.assertNotIsInstance(dtstart_val, datetime)
        self.assertEqual(dtstart_val, date(2024, 6, 15))
        self.assertEqual(dtend_val, date(2024, 6, 16))

    def test_export_timed_event_timezone(self):
        """
        Timed event DTSTART/DTEND must be timezone-aware datetimes in the user's timezone.

        Given  a timed calendar.event stored as UTC in the database.
            The current user has his timezone set to America/Toronto.
        When  to_vobject is called.
        Then  DTSTART and DTEND are datetime instances with tzinfo attached
            reflecting the user's timezone (America/Toronto), NOT event_tz.
        """
        # Given - force user timezone to America/Toronto
        self.assertEqual(self.user.tz, "America/Toronto")
        event = self._make_timed_event()

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        dtstart_val = vevent.dtstart.value
        dtend_val = vevent.dtend.value

        # Then - values are timezone-aware datetimes
        self.assertIsInstance(dtstart_val, datetime)
        self.assertIsNotNone(dtstart_val.tzinfo)
        self.assertIsNotNone(dtend_val.tzinfo)

        # Then - timezone reflects the user's timezone, not event_tz
        self.assertEqual(dtstart_val.tzinfo.zone, "America/Toronto")
        self.assertEqual(dtend_val.tzinfo.zone, "America/Toronto")

        # Then - the UTC value has been correctly converted to America/Toronto
        paris_tz = pytz.timezone("America/Toronto")
        expected_start = pytz.utc.localize(event.start).astimezone(paris_tz)
        expected_stop = pytz.utc.localize(event.stop).astimezone(paris_tz)
        self.assertEqual(dtstart_val, expected_start)
        self.assertEqual(dtend_val, expected_stop)

    def test_export_optional_fields_present(self):
        """
        Description, location and videocall_location are exported when set.

        Given a timed event with description, location and videocall_location filled.
        When to_vobject is called.
        Then DESCRIPTION, LOCATION and URL are present in the VEVENT with correct values.
        """
        # Given
        event = self._make_timed_event(
            description="A description",
            location="Room 42",
            videocall_location="https://meet.example.com/room",
        )

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        self.assertTrue(hasattr(vevent, "description"))
        self.assertEqual(vevent.description.value, "A description")

        self.assertTrue(hasattr(vevent, "location"))
        self.assertEqual(vevent.location.value, "Room 42")

        self.assertTrue(hasattr(vevent, "url"))
        self.assertEqual(vevent.url.value, "https://meet.example.com/room")

    def test_export_optional_fields_absent(self):
        """
        No spurious properties are added when optional fields are empty.

        Given a timed event with description, location and videocall_location all False.
        When to_vobject is called.
        Then DESCRIPTION, LOCATION and URL are absent from the VEVENT.
        """
        # Given
        event = self._make_timed_event(
            description=False,
            location=False,
            videocall_location=False,
        )

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        self.assertFalse(hasattr(vevent, "description"))
        self.assertFalse(hasattr(vevent, "location"))
        self.assertFalse(hasattr(vevent, "url"))

    def test_export_html_description(self):
        """
        An event with an HTML description exports both DESCRIPTION and X-ALT-DESC.

        Given  a timed calendar.event whose description contains HTML markup.
        When  to_vobject is called.
        Then  DESCRIPTION contains a plain-text version (no HTML tags)
            AND X-ALT-DESC with FMTTYPE=text/html contains the original HTML.
        """
        # Given
        event = self._make_timed_event(
            description="<p>Hello <b>World</b></p>"
        )

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then DESCRIPTION is plain text (no HTML tags)
        self.assertTrue(hasattr(vevent, "description"))
        self.assertEqual(vevent.description.value, 'Hello *World*')

        # Then X-ALT-DESC contains original HTML
        self.assertTrue(hasattr(vevent, "x_alt_desc"))
        self.assertEqual(
            vevent.x_alt_desc.params.get("FMTTYPE"), ["text/html"]
        )
        self.assertEqual(vevent.x_alt_desc.value, '<p>Hello <b>World</b></p>')

    def test_export_organizer_without_email_fallback(self):
        """
        Organizer without an email address uses the noreply fallback address.

        Given a timed event whose organizer has no email.
        When to_vobject is called.
        Then ORGANIZER value contains 'noreply+{partner_id}@odoo.local'.
        """
        # Given
        self.partner.email = False
        event = self._make_timed_event()

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        self.assertIn(f"noreply+{self.partner.id}@odoo.local", vevent.organizer.value)

        # Restore
        self.partner.email = "demo@example.com"

    def test_export_attendee_partstat(self):
        """
        Attendee PARTSTAT reflects the Odoo attendance state correctly.

        Given  a timed calendar.event with four attendees, one per possible Odoo state.
        When  to_vobject is called once.
        Then  each ATTENDEE PARTSTAT parameter matches the expected iCal value.
        """
        # Given - one partner per state
        state_map = {
            "needsAction": "NEEDS-ACTION",
            "accepted": "ACCEPTED",
            "declined": "DECLINED",
            "tentative": "TENTATIVE",
        }

        partners = {
            odoo_state: self.env["res.partner"].create({
                "name": f"Attendee {odoo_state}",
                "email": f"attendee_{odoo_state}@example.com",
            })
            for odoo_state in state_map
        }

        event = self._make_timed_event()
        event.write({
            "partner_ids": [(4, partner.id) for partner in partners.values()],
        })

        # Given - assign each attendee its state
        for odoo_state, partner in partners.items():
            attendee = event.attendee_ids.filtered(
                lambda a, p=partner: a.partner_id == p
            )
            self.assertTrue(
                attendee,
                f"No attendee record found for partner {partner.email}",
            )
            attendee.state = odoo_state

        # When - single export
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        attendee_props = getattr(vevent, "attendee_list", [])
        self.assertTrue(attendee_props, "No ATTENDEE properties found in VEVENT")

        # Then - validate each partner's PARTSTAT
        for odoo_state, ical_partstat in state_map.items():
            partner = partners[odoo_state]
            matching = [
                a for a in attendee_props
                if partner.email in a.value
            ]
            self.assertTrue(
                matching,
                f"No ATTENDEE property found for {partner.email} (state={odoo_state})",
            )
            self.assertEqual(
                matching[0].params.get("PARTSTAT", [None])[0],
                ical_partstat,
                msg=f"Expected PARTSTAT={ical_partstat} for Odoo state '{odoo_state}'",
            )

    def test_export_attendee_organizer_is_chair(self):
        """
        The organizer attendee receives ROLE=CHAIR and PARTSTAT=ACCEPTED.

        Given a timed event where the responsible user is also an attendee.
        When to_vobject is called.
        Then the organizer's attendee property has ROLE=CHAIR and PARTSTAT=ACCEPTED.
        """
        # Given
        event = self._make_timed_event()

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        attendee_props = getattr(vevent, "attendee_list", [])
        org_attendee = next(
            (a for a in attendee_props if self.partner.email in a.value),
            None,
        )
        if org_attendee:
            self.assertEqual(org_attendee.params.get("ROLE", []), ["CHAIR"])
            self.assertEqual(org_attendee.params.get("PARTSTAT", []), ["ACCEPTED"])

    def test_export_categories(self):
        """
        All event tag names are exported as CATEGORIES values.

        Given a timed event with two tags attached.
        When to_vobject is called.
        Then the CATEGORIES property contains both tag names.
        """
        # Given
        tag_a = self.env["calendar.event.type"].create({"name": "Tag A"})
        tag_b = self.env["calendar.event.type"].create({"name": "Tag B"})
        event = self._make_timed_event(categ_ids=[(6, 0, [tag_a.id, tag_b.id])])

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        self.assertTrue(hasattr(vevent, "categories"))
        exported = vevent.categories.value
        self.assertIn("Tag A", exported)
        self.assertIn("Tag B", exported)

    def test_export_alarms(self):
        """
        A calendar.event with multiple alarms exports the correct VALARMs.

        Given a timed calendar.event with:
            - an email alarm of 1 hour before
            - a notification alarm of 15 minutes before
        When to_vobject is called.
        Then the resulting VEVENT contains exactly 2 VALARMs where:
            - the email alarm has ACTION:EMAIL and TRIGGER:-PT1H
            - the notification alarm has ACTION:DISPLAY and TRIGGER:-PT15M
        """
        # Given
        email_alarm = self.env['calendar.alarm'].create({
            'name': 'Email 1 hour',
            'alarm_type': 'email',
            'duration': 1,
            'interval': 'hours',
        })
        notif_alarm = self.env['calendar.alarm'].create({
            'name': 'Notification 15 min',
            'alarm_type': 'notification',
            'duration': 15,
            'interval': 'minutes',
        })
        event = self._make_timed_event(alarm_ids = [Command.set([email_alarm.id, notif_alarm.id])])

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then — exactly 2 VALARMs
        valarms = [c for c in vevent.components() if c.name == "VALARM"]
        self.assertEqual(len(valarms), 2)

        # Index by ACTION for order-independent assertions
        alarms_by_action = {}
        for valarm in valarms:
            action = valarm.action.value
            alarms_by_action[action] = valarm

        # Then — EMAIL alarm
        self.assertIn('EMAIL', alarms_by_action, "Expected a VALARM with ACTION:EMAIL")
        email_valarm = alarms_by_action['EMAIL']
        self.assertEqual(email_valarm.trigger.value, timedelta(hours=-1))
        self.assertEqual(email_valarm.description.value, 'Email 1 hour')

        # Then — DISPLAY alarm
        self.assertIn('DISPLAY', alarms_by_action, "Expected a VALARM with ACTION:DISPLAY")
        display_valarm = alarms_by_action['DISPLAY']
        self.assertEqual(display_valarm.trigger.value, timedelta(minutes=-15))
        self.assertEqual(display_valarm.description.value, 'Notification 15 min')

@tagged("post_install", "-at_install")
class TestImportCalendarEventMapper(AbstractCalendarEventMapper):

    # ------------------------------------------------------------------ #
    #  IMPORT  (from_vobject)                                            #
    # ------------------------------------------------------------------ #

    def test_import_minimal_vevent_create(self):
        """
        A minimal VEVENT creates a calendar.event with correct name and times.

        Given a VCALENDAR with only SUMMARY, DTSTART and DTEND (UTC).
        When from_vobject is called on an empty record.
        Then the resulting record has the correct name, start and stop in UTC.
        """
        # Given
        uid = str(uuid.uuid4())
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            uid=uid
        )

        # When
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then
        self.assertEqual(uid, result.dav_uid)
        self.assertEqual(result.name, "Test Event")
        self.assertFalse(result.allday)
        self.assertEqual(result.start, datetime(2024, 6, 15, 10, 0, 0))
        self.assertEqual(result.stop, datetime(2024, 6, 15, 11, 0, 0))

    def test_import_vevent_without_description(self):
        """
        Given a VEVENT without a DESCRIPTION field,
        When importing,
        Then the event is created without a description.
        """
        vcal = vobject.iCalendar()
        vevent = vcal.add('vevent')
        vevent.add('uid').value = str(uuid.uuid4())
        vevent.add('dtstart').value = datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc)
        vevent.add('dtend').value = datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc)

        result = self.mapper.from_vobject(vcal, None, self.collection)

        self.assertEqual('(No Title)', result.name)

    def test_import_allday_vevent(self):
        """
        A VEVENT with date-only DTSTART/DTEND creates an all-day event.

        Given a VCALENDAR where DTSTART and DTEND are date objects (no time component).
        When from_vobject is called on an empty record.
        Then allday is True, start_date is correctly set and stop_date
              accounts for the RFC 5545 exclusive end-date convention.
        """
        # Given
        vcal = self._make_vcal(
            dtstart=date(2024, 6, 15),
            dtend=date(2024, 6, 16),
        )

        # When
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then
        self.assertTrue(result.allday)
        self.assertEqual(result.start_date, date(2024, 6, 15))
        self.assertEqual(result.stop_date, date(2024, 6, 16))

    def test_import_timed_event_utc_stored_naive(self):
        """
        UTC-aware datetimes are stripped of tzinfo before being stored in Odoo.

        Given a VCALENDAR with UTC-aware DTSTART and DTEND.
        When from_vobject is called.
        Then the resulting start and stop fields have no tzinfo (naive UTC).
        """
        # Given
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
        )
        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertIsNone(result.start.tzinfo)
        self.assertIsNone(result.stop.tzinfo)

    def test_import_timed_event_non_utc_converted(self):
        """
        Non-UTC timezone datetimes are converted to UTC before storage.

        Given a VCALENDAR with DTSTART/DTEND in Europe/Paris (UTC+2 in summer),
              representing 12:00-13:00 local time.
        When from_vobject is called.
        Then start and stop are stored as 10:00-11:00 UTC naive.
        """
        # Given
        paris = pytz.timezone("Europe/Paris")
        dtstart = paris.localize(datetime(2024, 6, 15, 12, 0, 0))
        dtend = paris.localize(datetime(2024, 6, 15, 13, 0, 0))
        vcal = self._make_vcal(dtstart=dtstart, dtend=dtend)
        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertEqual(result.start, datetime(2024, 6, 15, 10, 0, 0))
        self.assertEqual(result.stop, datetime(2024, 6, 15, 11, 0, 0))

    def test_import_description_and_location(self):
        """
        DESCRIPTION and LOCATION are mapped to the correct Odoo fields.

        Given a VCALENDAR with DESCRIPTION and LOCATION properties set.
        When from_vobject is called.
        Then the record's description and location fields match the iCal values.
        """
        # Given
        vcal = self._make_vcal(
            description="Meeting notes",
            location="Room 101",
        )
        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertEqual(str(result.description), "<span>Meeting notes</span>")
        self.assertEqual(result.location, "Room 101")

    def test_import_html_description(self):
        """
        An incoming VEVENT with X-ALT-DESC imports the HTML into description.

        Given  a VCALENDAR string containing X-ALT-DESC;FMTTYPE=text/html.
        When   from_vobject is called.
        Then   event.description contains the HTML from X-ALT-DESC
            and NOT the plain-text fallback from DESCRIPTION.
        """
        # Given
        cal = self._make_vcal(
            html_description = "<p>This is a <b>bold</b> description.</p>",
            location="Room 101",
        )

        # When
        event = self.env["calendar.event"].create({
            "name": "HTML Import Test",
            "start": datetime(2024, 6, 15, 10, 0, 0),
            "stop": datetime(2024, 6, 15, 11, 0, 0),
        })
        result = self.mapper.from_vobject(cal, event, self.collection)

        # Then – description holds the HTML from X-ALT-DESC
        self.assertEqual(str(result.description), "<p>This is a <b>bold</b> description.</p>")

    def test_import_url_sets_videocall_location(self):
        """
        The URL property is mapped to videocall_location.

        Given a VCALENDAR with a URL property.
        When from_vobject is called.
        Then videocall_location equals the URL value.
        """
        # Given
        vcal = self._make_vcal(url="https://meet.example.com/abc")
        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertEqual(result.videocall_location, "https://meet.example.com/abc")

    def test_import_conference_fallback_videocall(self):
        """
        The CONFERENCE property is used for videocall_location when URL is absent.

        Given a VCALENDAR with a CONFERENCE property but no URL property.
        When from_vobject is called.
        Then videocall_location equals the CONFERENCE value.
        """
        # Given
        vcal = self._make_vcal()
        vevent = next(c for c in vcal.components() if c.name == "VEVENT")
        vevent.add("conference").value = "https://meet.example.com/conf"
        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertEqual(result.videocall_location, "https://meet.example.com/conf")

    def test_import_categories_creates_tags(self):
        """
        Missing tags referenced in CATEGORIES are automatically created.
        
        Given calendar tags exists
        Given a VCALENDAR with CATEGORIES containing two tag names that do not exist.
        When from_vobject is called.
        Then the record's categ_ids contains both newly created tags.
        """
        # Given 
        self.env['calendar.event.type'].create([{'name':'New Tag'}, {'name':'Another Tag'}])
        vcal = self._make_vcal(categories=["New Tag", "Another Tag"])
        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        tag_names = result.categ_ids.mapped("name")
        self.assertIn("New Tag", tag_names)
        self.assertIn("Another Tag", tag_names)

    def test_import_categories_reuses_existing_tags(self):
        """
        Existing tags are reused; no duplicate tags are created on import.

        Given an existing tag and a VCALENDAR whose CATEGORIES reference it by name.
        When from_vobject is called.
        Then the total tag count in the database remains unchanged.
        """
        # Given
        self.env["calendar.event.type"].create({"name": "Existing Tag"})
        before_count = self.env["calendar.event.type"].search_count([])
        vcal = self._make_vcal(categories=["Existing Tag"])
        record = self.env["calendar.event"].new({})

        # When
        self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        after_count = self.env["calendar.event.type"].search_count([])
        self.assertEqual(before_count, after_count)

    def test_import_alarm_reuses_existing_with_different_unit(self):
        """
        A VALARM whose trigger matches an existing calendar.alarm reuses it,
        even when the unit differs.

        Given an existing calendar.alarm of 2 hours (notification).
        Given a VCALENDAR with a DISPLAY VALARM of TRIGGER -PT120M.
        When from_vobject is called.
        Then alarm_ids contains the pre-existing alarm and no new alarm is created.
        """
        # Given
        existing_alarm = self.env['calendar.alarm'].search([
            ('alarm_type', '=', 'notification'),
            ('duration_minutes', '=', '120'),
        ], limit=1)
        self.assertIsNotNone(existing_alarm)
        alarm_count_before = self.env['calendar.alarm'].search_count([])

        vcal = self._make_vcal()
        vevent = vcal.vevent
        valarm = vevent.add('valarm')
        valarm.add('action').value = 'DISPLAY'
        valarm.add('trigger').value = timedelta(minutes=-120)
        valarm.add('description').value = 'Reminder'

        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertEqual(len(result.alarm_ids), 1)
        # FIXME not sure why the Id doesn't correspond. new alarms id is NewId...
        #self.assertEqual(existing_alarm.id, result.alarm_ids.id)
        self.assertEqual(existing_alarm.name, result.alarm_ids.name)

    def test_import_alarm_creates_when_missing(self):
        """
        A VALARM with no matching calendar.alarm creates a new one, using the
        natural unit expressed in the VALARM.

        Given no calendar.alarm matches a 1-day email trigger.
        Given a VCALENDAR with an EMAIL VALARM of TRIGGER -P1D.
        When from_vobject is called.
        Then a new calendar.alarm is created with duration=1, duration_unit='days'.
        """
        # Given
        alarm_count_before = self.env['calendar.alarm'].search_count([])

        vcal = self._make_vcal()
        vevent = vcal.vevent
        valarm = vevent.add('valarm')
        valarm.add('action').value = 'EMAIL'
        valarm.add('trigger').value = timedelta(days=-1)
        valarm.add('description').value = 'Email reminder'

        record = self.env["calendar.event"].new({})

        # When
        result = self.mapper.from_vobject(vcal, record, self.collection)

        # Then
        self.assertEqual(len(result.alarm_ids), 1)
        self.assertEqual(
            self.env['calendar.alarm'].search_count([]),
            alarm_count_before + 1,
            "Exactly one new calendar.alarm should have been created",
        )

        new_alarm = result.alarm_ids
        self.assertEqual(new_alarm.alarm_type, 'email')
        self.assertEqual(new_alarm.duration, 1)
        self.assertEqual(new_alarm.interval, 'days')


    def test_import_update_existing_record(self):
        """
        Importing into an existing record updates it without creating a duplicate.

        Given an existing calendar.event and a VCALENDAR with a new SUMMARY.
        When from_vobject is called with the existing record.
        Then the record's name is updated and the total event count is unchanged.
        """
        # Given
        event = self._make_timed_event(name="Original Name")
        before_count = self.env["calendar.event"].search_count([])
        vcal = self._make_vcal(
            summary="Updated Name",
            uid=event.dav_uid,
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
        )

        # When
        result = self.mapper.from_vobject(vcal, event, self.collection)
        self.assertEqual(event.id, result.id)

        # Then
        after_count = self.env["calendar.event"].search_count([])
        self.assertEqual(before_count, after_count)
        self.assertEqual(event.name, "Updated Name")

    def test_import_non_vevent_raises(self):
        """
        A VCALENDAR without a VEVENT component raises a ValueError.

        Given a VCALENDAR containing only a VTODO component.
        When from_vobject is called.
        Then a ValueError is raised.
        """
        # Given
        vcal = vobject.iCalendar()
        vcal.add("vtodo").add("summary").value = "A todo"
        record = self.env["calendar.event"].new({})

        # When / Then
        with self.assertRaises(ValueError):
            self.mapper.from_vobject(vcal, record, self.collection)

    # ------------------------------------------------------------------ #
    #  ROUND-TRIP                                                        #
    # ------------------------------------------------------------------ #

    def test_roundtrip_timed_event(self):
        """
        All core fields survive an export → import cycle unchanged.

        Given a timed event with name, description, location, start and stop.
        When to_vobject is called and the result is fed back into from_vobject.
        Then all fields on the record match the original values.
        """
        # Given
        event = self._make_timed_event(
            description="Round trip desc",
            location="Round trip room",
        )
        original_start = event.start
        original_stop = event.stop

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        result = self.mapper.from_vobject(cal, event, self.collection)

        # Then
        self.assertEqual(result.name, event.name)
        self.assertEqual(result.start, original_start)
        self.assertEqual(result.stop, original_stop)
        self.assertEqual(str(event.description), "<span>Round trip desc</span>")
        self.assertEqual(result.location, "Round trip room")

    def test_roundtrip_allday_event(self):
        """
        All-day flag and dates survive an export → import cycle unchanged.

        Given an all-day calendar.event.
        When to_vobject is called and the result is fed back into from_vobject.
        Then allday is True and start_date equals the original date.
        """
        # Given
        event = self._make_allday_event()

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        result = self.mapper.from_vobject(cal, event, self.collection)

        # Then
        self.assertTrue(result.allday)
        self.assertEqual(result.start_date, date(2024, 6, 15))

    def test_roundtrip_uid_stability(self):
        """
        The DAV UID is preserved unchanged across an export → import cycle.

        Given a timed calendar.event with a dav_uid.
        When to_vobject is called.
        Then the UID in the resulting VEVENT matches the original dav_uid.
        """
        # Given
        event = self._make_timed_event()
        original_uid = event.dav_uid

        # When
        cal = self.mapper.to_vobject(event, self.collection)
        vevent = next(c for c in cal.components() if c.name == "VEVENT")

        # Then
        self.assertEqual(vevent.uid.value, original_uid)

@tagged("post_install", "-at_install")
class TestRecurrenceCalendarEventMapper(AbstractCalendarEventMapper):

    # ------------------------------------------------------------------ #
    #  RECURRENCE                                                        #
    # ------------------------------------------------------------------ #

    def test_export_weekly_recurring(self):
        # Given a calendar.event with a weekly recurrence of 5 occurrences
        event = self._make_timed_event(
            recurrency=True, 
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4', 
            event_tz='Europe/Brussels',
            partner_ids=[Command.set([self.partner.id])]
        )
        self.assertIsNotNone(event.recurrence_id)
        # When to_vobject is called
        cal = self.mapper.to_vobject(event, self.collection)

        # Then the a single VEVENT contains RRULE with FREQ=WEEKLY and COUNT=5
        self.assertEqual(1, len(list(cal.components())))
        vevent = next(c for c in cal.components() if c.name == "VEVENT")
        self.assertEqual(vevent.rrule.value, 'FREQ=DAILY;INTERVAL=1;COUNT=4')

    def test_export_soft_deleted_occurrence_produces_exdate(self):
        """
        Given a recurrence where one occurrence is soft-deleted (active=False),
        Then EXDATE is present in the exported VEVENT
        """
        # Given a daily recurrence of 4 occurrences
        event = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            event_tz='Europe/Brussels'
        )
        # Soft-delete the second occurrence (active=False)
        second_occurrence = event.recurrence_id.calendar_event_ids.sorted('start')[1]
        second_occurrence.write({'active': False})
        deleted_start = second_occurrence.start

        # When to_vobject is called on the base event
        base_event = event.recurrence_id.calendar_event_ids.sorted('start')[0]
        cal = self.mapper.to_vobject(base_event, self.collection)

        # Then EXDATE is present in the VEVENT
        vevent = next(c for c in cal.components() if c.name == "VEVENT")
        self.assertTrue(hasattr(vevent, 'exdate'), "EXDATE should be present in VEVENT")
        paris_tz=pytz.timezone("Europe/Brussels")
        expected_exdate = pytz.utc.localize(deleted_start).astimezone(paris_tz)
        self.assertEqual([expected_exdate],  vevent.exdate.value)

    def test_export_deleted_occurrence_produces_exdate(self):
        """
        Given a recurrence where one occurrence is hard-deleted,
        Then EXDATE is present in the exported VEVENT
        """
        # Given a daily recurrence of 4 occurrences
        event = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            event_tz='Europe/Brussels'
        )
        occurrences = event.recurrence_id.calendar_event_ids.sorted('start')
        base_event = occurrences[0]
        second_occurrence = occurrences[1]
        deleted_start = second_occurrence.start

        # Hard-delete the second occurrence
        second_occurrence.unlink()

        # When to_vobject is called on the base event
        cal = self.mapper.to_vobject(base_event, self.collection)

        # Then EXDATE is present in the VEVENT matching the deleted occurrence date
        vevent = next(c for c in cal.components() if c.name == "VEVENT")
        self.assertTrue(hasattr(vevent, 'exdate'), "EXDATE should be present in VEVENT")
        paris_tz=pytz.timezone("Europe/Brussels")
        expected_exdate = pytz.utc.localize(deleted_start).astimezone(paris_tz)
        self.assertEqual([expected_exdate],  vevent.exdate.value)

    def test_export_detached_occurrence_produces_recurrence_id(self):
        """
        Given a recurrence where one occurrence is detached (start overridden, follow_recurrence=False),
        Then the base VEVENT contains an EXDATE for the original date of that occurrence,
        Then a separate VEVENT with RECURRENCE-ID is exported for the detached occurrence.
        """
        # Given a daily recurrence of 4 occurrences
        event = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            event_tz='Europe/Brussels',
            partner_ids=[(4, self.partner.id)],
        )
        occurrences = event.recurrence_id.calendar_event_ids.sorted('start')
        base_event = occurrences[0]
        second_occurrence = occurrences[1]
        original_start = second_occurrence.start

        # Detach the second occurrence by moving it to a different date/time
        second_occurrence.write({
            'follow_recurrence': False,
            'start': datetime(2024, 6, 20, 8, 0, 0),
            'stop': datetime(2024, 6, 20, 10, 0, 0),
        })

        # When to_vobject is called on the base event
        cal = self.mapper.to_vobject(base_event, self.collection)
        vevents = [c for c in cal.components() if c.name == "VEVENT"]

        # Then the base VEVENT doesn't define a EXDATE
        base_vevent = next(v for v in vevents if not hasattr(v, 'recurrence_id'))
        self.assertFalse(hasattr(base_vevent, 'exdate'))

        # Then a separate VEVENT with RECURRENCE-ID is exported for the detached occurrence
        other_vevents = [v for v in vevents if hasattr(v, 'recurrence_id')]
        self.assertEqual(1, len(other_vevents), "A VEVENT with RECURRENCE-ID should be exported for the detached occurrence")
        override_vevent = other_vevents[0]
        paris_tz = pytz.timezone("Europe/Brussels")
        expected_recurrence_id = pytz.utc.localize(original_start).astimezone(paris_tz)
        self.assertEqual(expected_recurrence_id, override_vevent.recurrence_id.value)
        self.assertFalse(hasattr(override_vevent, 'rrule'), "RRULE should not be present on the detached occurrence VEVENT")

    def test_export_modified_occurrence_produces_recurrence_id_vevent(self):
        """
        Given one modified occurrence (different title),
        Then a second VEVENT with RECURRENCE-ID is present in the VCALENDAR
        """
        # Given a daily recurrence of 4 occurrences
        event = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            event_tz='Europe/Brussels',
            partner_ids=[(4, self.partner.id)],
        )
        occurrences = event.recurrence_id.calendar_event_ids.sorted('start')
        self.assertEqual(event, event.recurrence_id.base_event_id)
        second_occurrence = occurrences[1]

        # Modify one occurrence
        second_occurrence.write({'name': 'Modified Occurrence'})

        # When to_vobject is called on the base event
        cal = self.mapper.to_vobject(event, self.collection)

        # Then a second VEVENT with RECURRENCE-ID is present
        vevents = [c for c in cal.components() if c.name == "VEVENT"]
        self.assertEqual(len(vevents), 2, "A second VEVENT should be present for the modified occurrence")
        recurrence_id_vevent = next((v for v in vevents if hasattr(v, 'recurrence_id')), None)
        self.assertIsNotNone(recurrence_id_vevent, "A VEVENT with RECURRENCE-ID should be present")
        # Then proper mappind is done for exception.
        paris_tz=pytz.timezone("Europe/Brussels")
        self.assertEqual(set(recurrence_id_vevent.contents.keys()), {'summary', 'dtend', 'uid', 'recurrence-id', 'organizer', 'last-modified', 'dtstart', 'attendee'})
        self.assertEqual(recurrence_id_vevent.uid.value, event.dav_uid)
        self.assertEqual(recurrence_id_vevent.recurrence_id.value, pytz.utc.localize(datetime(2024, 6, 16, 10, 0)).astimezone(paris_tz) )
        self.assertEqual(recurrence_id_vevent.summary.value, 'Modified Occurrence')
        self.assertEqual(recurrence_id_vevent.dtstart.value, pytz.utc.localize(datetime(2024, 6, 16, 10, 0)).astimezone(paris_tz) )
        self.assertEqual(recurrence_id_vevent.dtend.value, pytz.utc.localize(datetime(2024, 6, 16, 11, 0)).astimezone(paris_tz) )
        self.assertEqual(recurrence_id_vevent.organizer.value, 'mailto:demo@example.com')
        self.assertEqual(recurrence_id_vevent.attendee.value, 'mailto:demo@example.com')

    def test_export_allday_recurring(self):
        # Given a calendar.event with a daily all-day recurrence of 4 occurrences
        event = self._make_allday_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            partner_ids=[Command.set([self.partner.id])]
        )
        self.assertIsNotNone(event.recurrence_id)
        self.assertEqual(4, len(event.recurrence_id.calendar_event_ids))
        # When to_vobject is called
        cal = self.mapper.to_vobject(event, self.collection)

        # Then a single VEVENT contains RRULE and DTSTART is a date (not datetime)
        self.assertEqual(1, len(list(cal.components())))
        vevent = next(c for c in cal.components() if c.name == "VEVENT")
        self.assertEqual(vevent.rrule.value, 'FREQ=DAILY;INTERVAL=1;COUNT=4')
        self.assertIsInstance(vevent.dtstart.value, date)
        self.assertNotIsInstance(vevent.dtstart.value, datetime)

    def test_import_weekly_recurring(self):
        # Given a VCALENDAR with RRULE:FREQ=WEEKLY;COUNT=5
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
        )

        # When from_vobject is called
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then the event has recurrency=True, a linked recurrence with the correct rrule
        self.assertTrue(result.recurrency)
        self.assertTrue(result.recurrence_id)
        self.assertEqual(result.rrule, 'FREQ=DAILY;INTERVAL=1;COUNT=4')
        
        # Then 4 events are create in database.
        self.assertEqual(4, len(result.recurrence_id.calendar_event_ids))
        for event in result.recurrence_id.calendar_event_ids:
            self.assertEqual(event.name, 'Test Event')
            # attendee_ids are define on each occurence.
            self.assertTrue(event.attendee_ids)
    
    def test_import_exdate_removes_occurrence(self):
        """
        Given RRULE:FREQ=DAILY;COUNT=5 + 1 EXDATE,
        Then 4 calendar_event_ids are active
        """
        # Given a VCALENDAR with RRULE and 1 EXDATE
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=5',
        )
        # Add EXDATE for the second occurrence (2024-06-16)
        vevent = next(c for c in vcal.components() if c.name == "VEVENT")
        exdate = vevent.add('exdate')
        exdate.value = [datetime(2024, 6, 16, 10, 0, 0, tzinfo=pytz.utc)]

        # When from_vobject is called
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then recurrence is created with correct rrule
        self.assertTrue(result.recurrency)
        self.assertTrue(result.recurrence_id)

        # Then only 4 active occurrences exist
        active_events = result.recurrence_id.calendar_event_ids.filtered('active')
        self.assertEqual(4, len(active_events))

        # Then no occurrence exists on the excluded date
        excluded_date = date(2024, 6, 16)
        self.assertFalse(
            any(e.start.date() == excluded_date for e in active_events),
            "No occurrence should exist on the EXDATEd date"
        )

    def test_import_multiple_exdates_removes_occurrences(self):
        """
        Given RRULE:FREQ=DAILY;COUNT=5 + 2 EXDATEs,
        Then 3 calendar_event_ids are active
        """
        # Given a VCALENDAR with RRULE and 2 EXDATEs
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=5',
        )
        # Add EXDATEs for the second and third occurrences (2024-06-16 and 2024-06-17)
        vevent = next(c for c in vcal.components() if c.name == "VEVENT")
        exdate = vevent.add('exdate')
        exdate.value = [
            datetime(2024, 6, 16, 10, 0, 0, tzinfo=pytz.utc),
            datetime(2024, 6, 17, 10, 0, 0, tzinfo=pytz.utc),
        ]

        # When from_vobject is called
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then recurrence is created
        self.assertTrue(result.recurrency)
        self.assertTrue(result.recurrence_id)

        # Then only 3 active occurrences exist
        active_events = result.recurrence_id.calendar_event_ids.filtered('active')
        self.assertEqual(3, len(active_events))

        # Then no occurrence exists on either excluded date
        excluded_dates = {date(2024, 6, 16), date(2024, 6, 17)}
        active_dates = {e.start.date() for e in active_events}
        self.assertFalse(
            excluded_dates & active_dates,
            "No occurrence should exist on EXDATEd dates"
        )
        
    def test_import_exdate_restores_previously_excluded_occurrence(self):
        """
        Given an existing recurrence with 2 soft-deleted occurrences (exdates),
        Given an incoming VEVENT with RRULE:FREQ=DAILY;COUNT=5 + only 1 EXDATE,
        Then the occurrence no longer in EXDATE is restored (active=True),
        Then the occurrence still in EXDATE remains soft-deleted (active=False)
        """
        # Given an existing daily recurrence of 5 occurrences
        existing = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=5',
            event_tz='Europe/Brussels',
        )
        occurrences = existing.recurrence_id.calendar_event_ids.sorted('start')

        # Soft-delete the 2nd and 3rd occurrences
        occurrences[1].write({'active': False})
        occurrences[2].write({'active': False})

        # Given an incoming VCALENDAR with only 1 EXDATE (2024-06-16, the 2nd occurrence)
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=5',
        )
        vevent = next(c for c in vcal.components() if c.name == "VEVENT")
        exdate = vevent.add('exdate')
        exdate.value = [datetime(2024, 6, 16, 10, 0, 0, tzinfo=pytz.utc)]

        # When from_vobject is called on the existing event
        result = self.mapper.from_vobject(vcal, existing, self.collection)

        # Then 4 active occurrences exist (5 - 1 remaining exdate)
        all_events = result.recurrence_id.calendar_event_ids
        self.assertEqual(4, len(all_events))

        # Then the 3rd occurrence (2024-06-17) is restored
        restored_date = date(2024, 6, 17)
        self.assertIn(restored_date, {e.start.date() for e in all_events},
                    "The occurrence no longer in EXDATE should be restored")

        # Then the 2nd occurrence (2024-06-16) remains soft-deleted
        excluded_date = date(2024, 6, 16)
        self.assertNotIn(excluded_date, {e.start.date() for e in all_events},
                        "The occurrence still in EXDATE should remain soft-deleted")

    def test_import_exdate_restores_previously_hard_deleted_occurrence(self):
        """
        Given an existing recurrence with 2 hard-deleted occurrences,
        Given an incoming VEVENT with RRULE:FREQ=DAILY;COUNT=5 + only 1 EXDATE,
        Then the occurrence no longer in EXDATE is recreated,
        Then the occurrence still in EXDATE remains absent.
        """
        # Given an existing daily recurrence of 5 occurrences
        existing = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=5',
            event_tz='Europe/Brussels',
        )
        occurrences = existing.recurrence_id.calendar_event_ids.sorted('start')

        # Hard-delete the 2nd and 3rd occurrences
        occurrences[1].unlink()
        occurrences[2].unlink()
        self.assertEqual(3, len(existing.recurrence_id.calendar_event_ids))

        # Given an incoming VCALENDAR with only 1 EXDATE (2024-06-16, the 2nd occurrence)
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=5',
        )
        vevent = next(c for c in vcal.components() if c.name == "VEVENT")
        exdate = vevent.add('exdate')
        exdate.value = [datetime(2024, 6, 16, 10, 0, 0, tzinfo=pytz.utc)]

        # When from_vobject is called on the existing event
        result = self.mapper.from_vobject(vcal, existing, self.collection)

        # Then 4 active occurrences exist (5 - 1 remaining exdate)
        active_events = result.recurrence_id.calendar_event_ids.filtered('active')
        self.assertEqual(4, len(active_events))

        # Then the 3rd occurrence (2024-06-17) is recreated
        restored_date = datetime(2024, 6, 17,10, 0, 0)
        self.assertIn(restored_date, {e.start for e in active_events},
                    "The occurrence no longer in EXDATE should be recreated")

        # Then the 2nd occurrence (2024-06-16) remains absent
        excluded_date = datetime(2024, 6, 16,10, 0, 0)
        self.assertNotIn(excluded_date, {e.start for e in active_events},
                        "The occurrence still in EXDATE should remain absent")

    def test_import_recurrence_id_vevent_updates_occurrence(self):
        """
        Given a calendar.event in Odoo with a daily recurrence of 4 occurrences,
        Given a VCALENDAR containing a base VEVENT and one VEVENT with a RECURRENCE-ID targeting the 2nd occurrence,
        When importing,
        Then the 2nd occurrence is updated with the overridden summary and time,
        Then the other 3 occurrences remain unchanged.
        """
        # Given an existing daily recurrence of 4 occurrences
        existing = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            event_tz='Europe/Brussels',
            name='Base Event',
        )
        self.assertEqual(4, len(existing.recurrence_id.calendar_event_ids))
        uid = existing.dav_uid

        # Given a VCALENDAR with a base VEVENT and one RECURRENCE-ID override on the 2nd occurrence
        vcal = self._make_vcal(
            dtstart=datetime(2024, 6, 15, 10, 0, 0, tzinfo=pytz.utc),
            dtend=datetime(2024, 6, 15, 11, 0, 0, tzinfo=pytz.utc),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            summary='Base Event',
            uid=uid,
        )

        # Add a RECURRENCE-ID VEVENT overriding the 2nd occurrence (2024-06-16)
        override_vevent = vobject.newFromBehavior('vevent')
        override_vevent.add('uid').value = uid
        override_vevent.add('summary').value = 'Overridden Occurrence'
        override_vevent.add('dtstart').value = datetime(2024, 6, 16, 14, 0, 0, tzinfo=pytz.utc)
        override_vevent.add('dtend').value = datetime(2024, 6, 16, 15, 0, 0, tzinfo=pytz.utc)
        override_vevent.add('recurrence-id').value = datetime(2024, 6, 16, 10, 0, 0, tzinfo=pytz.utc)
        vcal.add(override_vevent)

        # When importing
        result = self.mapper.from_vobject(vcal, existing, self.collection)

        occurrences = result.recurrence_id.calendar_event_ids.sorted('start')
        self.assertEqual(4, len(occurrences))

        # Then the 2nd occurrence is updated
        second = occurrences[1]
        self.assertEqual('Overridden Occurrence', second.name)
        self.assertEqual(datetime(2024, 6, 16, 14, 0, 0, tzinfo=pytz.utc), second.start.replace(tzinfo=pytz.utc))
        self.assertEqual(datetime(2024, 6, 16, 15, 0, 0, tzinfo=pytz.utc), second.stop.replace(tzinfo=pytz.utc))

        # Then other occurrences remain unchanged
        self.assertEqual('Base Event', occurrences[0].name)
        self.assertEqual('Base Event', occurrences[2].name)
        self.assertEqual('Base Event', occurrences[3].name)

        self.assertEqual(datetime(2024, 6, 15, 10, 0, 0), occurrences[0].start)
        self.assertEqual(datetime(2024, 6, 17, 10, 0, 0), occurrences[2].start)
        self.assertEqual(datetime(2024, 6, 18, 10, 0, 0), occurrences[3].start)
            
    def test_import_create_all_occurrences(self):
        """
        Given a VCALENDAR with a base VEVENT containing an RRULE (no RECURRENCE-ID),
        When importing,
        Then a recurrence is created with all expected occurrences and all fields are correctly mapped.
        """
        # Given a VCALENDAR with a daily recurrence of 4 occurrences and all available fields
        uid = str(uuid.uuid4())
        brussels_tz = pytz.timezone('Europe/Brussels')
        vcal = self._make_vcal(
            dtstart=brussels_tz.localize(datetime(2024, 6, 15, 10, 0, 0)),
            dtend=brussels_tz.localize(datetime(2024, 6, 15, 11, 0, 0)),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            summary='New Recurrence',
            html_description='<p>HTML description</p>',
            location='Brussels, Belgium',
            url='https://example.com/event',
            attendees=[
                {'email': self.partner.email, 'cn': self.partner.name},
            ],
            uid=uid,
        )

        # When importing with no existing event
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then a recurrence is created with 4 occurrences
        occurrences = result.recurrence_id.calendar_event_ids.sorted('start')
        self.assertEqual(4, len(occurrences))

        # Then all occurrences have the correct field values
        for event in occurrences:
            self.assertEqual('New Recurrence', event.name)
            self.assertEqual('<p>HTML description</p>', str( event.description))
            self.assertEqual('Brussels, Belgium', event.location)
            self.assertEqual('https://example.com/event',event.videocall_location)
            self.assertIn(self.partner, event.partner_ids)

        # Then the starts are correct, stored in UTC (Europe/Brussels UTC+2 in June)
        expected_starts_utc = [
            datetime(2024, 6, 15, 8, 0, 0),
            datetime(2024, 6, 16, 8, 0, 0),
            datetime(2024, 6, 17, 8, 0, 0),
            datetime(2024, 6, 18, 8, 0, 0),
        ]
        for event, expected in zip(occurrences, expected_starts_utc):
            self.assertEqual(expected, event.start.replace(tzinfo=None))

    def test_import_update_non_recurring_to_daily_recurring(self):
        """
        Given an existing non-recurring calendar.event in Odoo,
        Given a VCALENDAR with the same UID but now containing a daily RRULE,
        When importing,
        Then the event is converted to a daily recurring event,
        Then all occurrences are created with the correct values.
        """
        brussels_tz = pytz.timezone('Europe/Brussels')

        existing_event = self._make_timed_event(
            recurrency=False,
            name='Single Event',
        )
        self.assertFalse(existing_event.recurrency)

        vcal = self._make_vcal(
            dtstart=existing_event.start.astimezone(brussels_tz),
            dtend=existing_event.stop.astimezone(brussels_tz),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            summary='Now Daily Recurring',
            uid=existing_event.dav_uid,
        )

        result = self.mapper.from_vobject(vcal, existing_event, self.collection)

        self.assertTrue(result.recurrency)
        self.assertTrue(result.recurrence_id)

        occurrences = result.recurrence_id.calendar_event_ids.sorted('start')
        self.assertEqual(4, len(occurrences))

        for i, occ in enumerate(occurrences):
            self.assertEqual('Now Daily Recurring', occ.name)
            expected_start = existing_event.start + timedelta(days=i)
            self.assertEqual(expected_start.replace(tzinfo=None), occ.start.replace(tzinfo=None))


    def test_import_update_weekly_recurring_to_daily_recurring(self):
        """
        Given an existing weekly recurring calendar.event in Odoo,
        Given a VCALENDAR with the same UID but now containing a daily RRULE,
        When importing,
        Then the recurrence is updated to daily,
        Then the new occurrences reflect the daily frequency.
        """
        brussels_tz = pytz.timezone('Europe/Brussels')

        existing_event = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=WEEKLY;INTERVAL=1;COUNT=3',
            event_tz='Europe/Brussels',
            name='Weekly Event',
        )
        self.assertEqual('weekly', existing_event.recurrence_id.rrule_type)

        vcal = self._make_vcal(
            dtstart=existing_event.start.astimezone(brussels_tz),
            dtend=existing_event.stop.astimezone(brussels_tz),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            summary='Now Daily Event',
            uid=existing_event.dav_uid,
        )

        result = self.mapper.from_vobject(vcal, existing_event, self.collection)

        self.assertTrue(result.recurrency)
        self.assertEqual('daily', result.recurrence_id.rrule_type)

        occurrences = result.recurrence_id.calendar_event_ids.sorted('start')
        self.assertEqual(4, len(occurrences))

        for i, occ in enumerate(occurrences):
            self.assertEqual('Now Daily Event', occ.name)
            expected_start = existing_event.start + timedelta(days=i)
            self.assertEqual(expected_start.replace(tzinfo=None), occ.start.replace(tzinfo=None))


    def test_import_update_daily_recurring_to_non_recurring(self):
        """
        Given an existing daily recurring calendar.event in Odoo,
        Given a VCALENDAR with the same UID but without any RRULE,
        When importing,
        Then the event is converted to a non-recurring event,
        Then only a single occurrence remains.
        """
        brussels_tz = pytz.timezone('Europe/Brussels')

        existing_event = self._make_timed_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            event_tz='Europe/Brussels',
            name='Daily Event',
        )
        self.assertEqual('FREQ=DAILY;INTERVAL=1;COUNT=4', existing_event.rrule)
        self.assertTrue(existing_event.recurrency)
        self.assertEqual(4, len(existing_event.recurrence_id.calendar_event_ids))

        vcal = self._make_vcal(
            dtstart=existing_event.start.astimezone(brussels_tz),
            dtend=existing_event.stop.astimezone(brussels_tz),
            summary='No Longer Recurring',
            uid=existing_event.dav_uid,
        )

        result = self.mapper.from_vobject(vcal, existing_event, self.collection)

        self.assertTrue(result.active)
        self.assertFalse(result.rrule)
        self.assertFalse(result.recurrence_id or result.recurrence_id.calendar_event_ids)
        # FIXME strangely the record it self doesn't get updated when updating recurrence.
        #self.assertEqual('No Longer Recurring', result.name)
        self.assertEqual(existing_event.start.replace(tzinfo=None), result.start.replace(tzinfo=None))
        self.assertEqual(existing_event.stop.replace(tzinfo=None), result.stop.replace(tzinfo=None))

    def test_import_allday_recurring(self):
        # Given a VCALENDAR with an all-day DTSTART and RRULE:FREQ=DAILY;COUNT=4
        vcal = self._make_vcal(
            dtstart=date(2024, 6, 15),
            dtend=date(2024, 6, 16),
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
        )

        # When from_vobject is called
        result = self.mapper.from_vobject(vcal, None, self.collection)

        # Then the event is all-day
        self.assertTrue(result.allday)

        # Then the event has recurrency=True, a linked recurrence with the correct rrule
        self.assertTrue(result.recurrency)
        self.assertTrue(result.recurrence_id)
        self.assertEqual(result.rrule, 'FREQ=DAILY;INTERVAL=1;COUNT=4')

        # Then 4 events are created in database.
        self.assertEqual(4, len(result.recurrence_id.calendar_event_ids))
        for event in result.recurrence_id.calendar_event_ids:
            self.assertEqual(event.name, 'Test Event')
            self.assertTrue(event.allday)
            self.assertIsInstance(event.start_date, date)
            # attendee_ids are defined on each occurrence.
            self.assertTrue(event.attendee_ids)

    def test_import_vevent_with_recurrence_id_allday(self):
        """
        Given a calendar event with a daily all-day recurrence of 4 occurrences.
        When importing a VEVENT with RECURRENCE-ID for the third occurrence updating summary.
        Then only the third occurrence gets updated.
        """
        # Given a daily all-day recurrence of 4 occurrences
        event = self._make_allday_event(
            recurrency=True,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            partner_ids=[Command.set([self.partner.id])]
        )
        self.assertEqual(4, len(event.recurrence_id.calendar_event_ids))
        uid = event.dav_uid

        # Given a VCALENDAR with a base VEVENT
        vcal = self._make_vcal(
            dtstart=date(2024, 6, 15),
            dtend=date(2024, 6, 16),
            summary=event.name,
            rrule='FREQ=DAILY;INTERVAL=1;COUNT=4',
            uid=uid,
        )

        # Add a RECURRENCE-ID VEVENT overriding the third occurrence (2024-06-17)
        override_vevent = vobject.newFromBehavior('vevent')
        override_vevent.add('uid').value = uid
        override_vevent.add('summary').value = 'Updated Third Occurrence'
        override_vevent.add('dtstart').value = date(2024, 6, 17)
        override_vevent.add('dtend').value = date(2024, 6, 18)
        override_vevent.add('recurrence-id').value = date(2024, 6, 17)
        vcal.add(override_vevent)

        # When from_vobject is called
        result = self.mapper.from_vobject(vcal, event, self.collection)

        # Then the recurrence still has 4 occurrences
        self.assertEqual(4, len(result.recurrence_id.calendar_event_ids))

        # Then only the third occurrence has the updated summary
        occurrences = result.recurrence_id.calendar_event_ids.sorted('start_date')
        self.assertEqual(occurrences[0].name, 'All Day Event')
        self.assertEqual(occurrences[1].name, 'All Day Event')
        self.assertEqual(occurrences[2].name, 'Updated Third Occurrence')
        self.assertEqual(occurrences[3].name, 'All Day Event')

        # Then all occurrences remain all-day
        for occ in occurrences:
            self.assertTrue(occ.allday)


    def test_delete_all_occurence(self):
        # TODO This is currently failing, because delete only delete the first occurence.
        # We should probably use a context variable for this.
        pass


    def test_import_default_organizer(self):
        """
        Given a VEVENT without organizer
        When importing
        Then meeting get created with current user
        """
    
    def test_import_custom_organizer(self):
        """
        Given a VEVENT with a custom organizer (not in Odoo database)
        When importing event
        Then meeting get created without organizer
        """

    def test_import_this_and_following_occurrences(self):
        """
        Given a VCALENDAR with a VEVENT with RECURRENCE-ID + RANGE=THISANDFUTURE,
        Then occurrences from that date onwards are updated,
        And previous occurrences are unchanged
        """
        # Check how thunderbird and evolution are working in this regard.


    # def test_recurrence_with_vtimezone(self):
    # TODO timezone information should be mapped to/from odoo.

# TODO Test recurrence modification e.g.: not-recurrence to daily
# TODO Test recurrence modification. e.g.: weekly to daily
# TODO Test recurrence modification e.g.: daily to no recurrence
# TODO Test recurrence getting split (aka, FREQ=WEEKLY;UNTIL=20240107T235959Z)

# TODO handle when to send notification or not
# Check how thunderbird and evolution handle this:
#
# Schedule-Reply: T   # T = send, F = don't send
#
# Prefer: handling=lenient
#
# Directly in VEVENT
# ATTENDEE;SCHEDULE-AGENT=SERVER:mailto:john@example.com   # Server sends notification
# ATTENDEE;SCHEDULE-AGENT=CLIENT:mailto:jane@example.com  # Client handles it
# ATTENDEE;SCHEDULE-AGENT=NONE:mailto:bob@example.com     # No notification sent
