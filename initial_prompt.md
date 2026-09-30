## Goal

This program is for anyone who wants to create a bubble sheet for multiple choice or partially
multiple choice exams and then have those bubble sheets graded with summary information available for export.

## Context

- Audience/users: Teachers or anyone administering an exam
- Environment: online service, maybe a digital ocean droplet
- Existing code: None

## Requirements

1. User can paste names as comma delimited or line break delimited or upload a CSV
2. User will define a class name and exam name at the minimum for QR code generation. They will also need to provide a key in CSV or PDF format.
3. Bubble sheets of any number can be created and can also have a space for
   written responses that are numbered. Should have a unique QR code that identifies student and class
4. Bubble sheets are uploaded as PDF or JPG/HEIC type. They can be done in one batch or individually until a user says they are done with uploads.
5. Results should be downloadable as CSV or PNG graphics of summary data
6. Information about students is not stored to most easily maintain COPPA compliance
7. If a user tries to proceed with empty data provide an error
8. If scantron bubble are not readable, tell the user which ones could not be read so they can manually verify
9. If a user does not upload a key, do not proceed with student reponse uploads

## Constraints

- Language/stack: not sure, but probably html/css/js and python on the backend
- Dependencies: use libraries for OCR or if the tool already exists that I want. Ask first.
- Style: commented and documented code
- Don't: make API keys public ever

## Definition of done

- I will know that it works when a batch of student successfully graded with their bubble sheets having been created by this program

## How to work

- Start by proposing a plan and file structure. Wait for my approval before writing code.
- Ask me if anything above is ambiguous rather than guessing.
- Build in small steps and run/test as you go.
