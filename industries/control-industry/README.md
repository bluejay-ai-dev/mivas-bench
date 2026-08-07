# control-industry

The Control Industry is a setup-and-validation check for MIVAS benchmark runs. When fully configured, it is an extremely simple multi-agent system — not a realistic customer voice-agent design.

It models a hypothetical business called **Bluejay's Repair Services**. The only thing callers can do is schedule a repair appointment.

## Flow

1. Starts with the **receptionist** agent
2. Hands off to the **scheduler** agent
3. The scheduler books a repair appointment using a single tool: **Schedule Appointment**

## Purpose

Use the Control Industry for every agent you want to benchmark with this repo. Spin it up to confirm you can get that agent to book an appointment. If that works, your MIVAS bench setup is wired correctly.

This industry does **not** mirror how customers build voice agents. It is a control test for validating that MIVAS is set up properly.

## Expected outcome

Your agent schedules a generic repair appointment, and evaluations show database state reflecting a scheduled appointment.
