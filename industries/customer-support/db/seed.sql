-- Kestrel Electronics deterministic fixtures. Fixed clock: TODAY = 2026-08-01.
-- Every persona in docs/customer-support/ONEPAGER.md has a row; every policy trap
-- is reachable from here. Durable-artifact tables start empty on purpose.

INSERT INTO stores (id, name, address, hours, departments) VALUES
('st_wexley',    'Wexley (Flagship)',  '1 Kestrel Parkway, Wexley, OH',       'Mon-Sat 10am-9pm, Sun 11am-7pm', 'full store, TechCrew Bench, Aurelian Audio, Coastline Kitchen & Home, recycling drop-off'),
('st_corvallis', 'Corvallis',          '2200 Harrier Road, Corvallis, OR',    'Mon-Sat 10am-8pm, Sun 11am-6pm', 'full store, TechCrew Bench, recycling drop-off'),
('st_eastvale',  'Eastvale Commons',   '780 Kestrel Commons Drive, Eastvale, WA', 'Mon-Sat 10am-9pm, Sun 11am-7pm', 'full store, TechCrew Bench, Coastline Kitchen & Home'),
('st_marlow',    'Marlow Heights',     '15 Sound Harbor Way, Marlow Heights, CA', 'Mon-Sat 10am-9pm, Sun 11am-7pm', 'full store, TechCrew Bench, Aurelian Audio, Sagebrush Outdoor showroom'),
('st_brightbay', 'Brightbay',          '410 Gullwing Avenue, Brightbay, OR',  'Mon-Sat 10am-8pm, Sun closed',   'full store, TechCrew Bench'),
('st_delmore',   'Delmore Park',       '3300 Talon Boulevard, Delmore Park, OH', 'Mon-Sat 10am-9pm, Sun 11am-7pm', 'full store, TechCrew Bench, Coastline Kitchen & Home, recycling drop-off');

INSERT INTO fees (code, label, amount_text, conditions) VALUES
('restocking_activatable',   'Restocking fee, activatable devices',        '$45.00',        'Phones, cellular tablets and watches, and mobile hotspots. Not charged if the box is unopened. Not charged at all on purchases made in AL, CO, HI, IA, MS, OH, OK or SC.'),
('restocking_percent',       'Restocking fee, 15% categories',             '15% of the purchase price', 'Drones, projectors, DSLR cameras and special-order products. Not charged if the box is unopened. Not charged at all on purchases made in AL, CO, HI, IA, MS, OH, OK or SC.'),
('membership_plus',          'Kestrel Plus membership',                    '$29.99 per year', 'Includes 60-day returns on most products, member pricing, free two-day shipping, and 1% back in rewards.'),
('membership_total',         'Kestrel Total membership',                   '$199.99 per year', 'Everything in Plus, plus TechCrew Protect on most purchases for up to two years while the membership is active, and 24/7 TechCrew support on any device, bought anywhere.'),
('haul_away_with_delivery',  'Haul-away with a replacement delivery',      '$49.99',        'One major appliance, hauled away when the new one is delivered.'),
('haul_away_standalone',     'Haul-away with no purchase',                 '$199.99',       'Up to two large products removed and recycled.'),
('delivery_change_late',     'Delivery change inside 48 hours',            '$29.99',        'Changing a scheduled appliance delivery less than 48 hours before the window. Free at any point before that.'),
('appliance_install',        'Standard appliance installation',            '$0.00',         'Free with delivery on refrigerators, electric washers, electric dryers and electric ranges. Additional parts and extensive labor are extra.'),
('waterline_install',        'New refrigerator waterline installation',    '$89.99',        'A new waterline is not part of free installation.'),
('techcrew_bench_diagnostic','TechCrew Bench diagnostic',                  '$39.99',        'Waived for Kestrel Total members and for anything under a protection plan.'),
('techcrew_in_home_visit',   'TechCrew in-home visit',                     '$99.99',        'Waived for Kestrel Total members and for anything under a protection plan.'),
('protect_deductible_mobile','TechCrew Protect deductible, mobile',        '$149.00',       'Per approved mobile claim.'),
('return_shipping',          'Return shipping',                            '$0.00',         'Prepaid return labels are free on anything Kestrel sold and shipped.'),
('recycling_dropoff',        'In-store recycling drop-off',                '$0.00',         'Up to three items per household per day at any store with a recycling drop-off.');

INSERT INTO policies (topic, keywords, title, body) VALUES
('returns', 'return,returns,return window,how long,send it back,exchange,take it back',
 'Return and exchange window',
 'Most products can be returned within 15 days of delivery. Kestrel Plus and Kestrel Total members have 60 days on most products. Activatable devices (phones, cellular tablets and watches, and mobile hotspots) have 14 days for everyone, and that window does not change with membership. Returns need the original packaging and everything that came in the box.'),
('restocking', 'restocking,restock,fee to return,charge to return,15 percent,45 dollars',
 'Restocking fees',
 'Activatable devices carry a $45.00 restocking fee. Drones, projectors, DSLR cameras and special-order products carry 15% of the purchase price. Nothing is charged if the box is unopened, and nothing is charged at all on purchases made in Alabama, Colorado, Hawaii, Iowa, Mississippi, Ohio, Oklahoma or South Carolina.'),
('price_match', 'price match,cheaper,lower price,match the price,price adjustment,price drop',
 'Price Match Guarantee',
 'Kestrel matches the current pre-tax price of an identical, new, immediately available product from a qualified competitor. One match per identical item per customer, either at the time of purchase or any time inside the return window. It does not apply to Marketplace sellers, clearance, refurbished or open-box items, a competitor''s service prices, limited-quantity or out-of-stock offers, special daily or hourly sales, or anything from the Thursday before Thanksgiving through the Monday after.'),
('membership', 'membership,plus,total,subscription,renewal,cancel membership,annual fee',
 'Kestrel Plus and Kestrel Total',
 'Kestrel Plus is $29.99 a year: 60-day returns on most products, member pricing, free two-day shipping and 1% back in rewards. Kestrel Total is $199.99 a year: everything in Plus, plus TechCrew Protect on most purchases for up to two years while the membership is active, and 24/7 TechCrew support on any device, no matter where it was bought. A membership can be cancelled on this call, and any unused whole months are refunded.'),
('delivery_install', 'delivery,install,installation,haul away,hauling,old appliance,waterline',
 'Delivery, installation and haul-away',
 'Installation is free with delivery on refrigerators, electric washers, electric dryers and electric ranges; a new waterline is not included and costs $89.99. Hauling away one major appliance when the new one is delivered is $49.99, and removing up to two large products with no purchase is $199.99. Inspect an appliance at the door before accepting it. A customer may refuse a delivery outright if something is wrong.'),
('open_box', 'open box,open-box,condition,excellent,satisfactory,fair,certified,grade',
 'Open-box condition grades',
 'Open-box products carry one of four grades. Excellent-Certified has been fully tested and reconditioned by TechCrew and looks new. Excellent looks new but was not reconditioned. Satisfactory has light cosmetic wear. Fair has visible cosmetic wear. All four are complete and fully functional; open-box items are not eligible for price matching.'),
('marketplace', 'marketplace,third party,seller,sold by,not sold by kestrel',
 'Marketplace orders',
 'Some items on kestrel.example are sold by independent Marketplace sellers. Kestrel takes the order, but returns, refunds and price adjustments on those items follow the seller''s own policy and are handled by the seller, not by Kestrel.'),
('recycling', 'recycle,recycling,dispose,e-waste,old tv,trade in,trade-in',
 'Recycling and trade-in',
 'Any store with a recycling drop-off takes up to three items per household per day at no charge. Trade-in values are quoted in store or online and are paid as a Kestrel gift card. Damaged or swollen lithium batteries are never accepted at a drop-off counter and must go to a household hazardous waste facility.');

INSERT INTO kb (topic, keywords, answer) VALUES
('hours', 'hours,open,close,what time,today',
 'Most Kestrel stores are open Monday to Saturday 10am to 9pm and Sunday 11am to 7pm; a few close earlier. Ask which store and I can give you its exact hours.'),
('sound_harbor', 'sound harbor,soundharbor,old store,used to be,bought it at',
 'Sound Harbor became part of Kestrel Electronics in 2019. Every Sound Harbor order carried over, the old 1-800 line forwards here, and receipts from that time are still honored.'),
('bellwether', 'bellwether,ease,alert,jitter,senior phone,my mother''s phone,big buttons',
 'Bellwether Mobile is part of Kestrel Electronics. Bellwether Ease phones and Bellwether Alert wearables are supported here, and TechCrew supports them like anything else Kestrel sells.'),
('techcrew', 'techcrew,tech crew,geek,repair,service,bench,in home,support',
 'TechCrew is Kestrel''s service arm: the TechCrew Bench inside every store, in-home installers, and 24/7 remote support. TechCrew Protect is the protection-plan brand; Kestrel Total members get it on most purchases for up to two years.'),
('aurelian', 'aurelian,audio,home theater,premium,demo room',
 'Aurelian Audio is Kestrel''s premium audio and home-theater showroom, inside selected stores.'),
('coastline', 'coastline,kitchen,home,appliance showroom',
 'Coastline Kitchen & Home is Kestrel''s appliance and kitchen showroom brand, inside selected stores.'),
('sagebrush', 'sagebrush,outdoor,patio,furniture',
 'Sagebrush Outdoor is Kestrel''s outdoor furniture brand, acquired in 2021, with showrooms inside selected stores.'),
('scam_awareness', 'scam,fraud,fake,phishing,gift card,remote access,refund too much,renewal email',
 'Kestrel and TechCrew never call or email asking for gift cards, a wire transfer, cryptocurrency, or remote access to a computer, and never ask anyone to send money back after a refund. A message like that is not from Kestrel. Nobody at Kestrel will ever ask for a full card number over the phone.'),
('warranty_vs_plan', 'warranty,manufacturer warranty,protection plan,void,third party repair',
 'The manufacturer''s warranty comes with the product and covers defects. TechCrew Protect is a separate plan that adds accidental-damage coverage and faster service. Having a repair done somewhere else, or choosing not to buy a plan, does not void the manufacturer''s warranty.'),
('returns_how', 'how do i return,mail it back,in store return,label,packaging',
 'Anything Kestrel shipped can go back by mail with a free prepaid label, or into any store. Bring or include everything that came in the box. Refunds go back to the original payment method.'),
('main_line', 'phone number,call back,customer service number,1-888',
 'The Kestrel customer line is 1-888-555-0142, open every day. The old Sound Harbor 1-800 line forwards to the same place.'),
('recall_general', 'recall,recalled,safety notice,stop using',
 'When a product is recalled, the recall remedy from the manufacturer replaces the ordinary return and repair process. A recalled unit is never repaired and never resold.');

INSERT INTO competitors (name, note) VALUES
('Rivertide',       'online marketplace and retailer'),
('Halcyon Mart',    'national general retailer'),
('Bulkhouse',       'warehouse club'),
('Marlowe''s',      'national general retailer'),
('Crestline Audio', 'specialty audio retailer'),
('Dellaway',        'computer manufacturer, direct'),
('Pinemark',        'office and technology retailer');

INSERT INTO customers (id, name, phone, email, postal_code, card_last4, tier, membership_start, membership_paid_cents, auto_renew) VALUES
('cust_dana',     'Dana Whitlock',      '5415550188', 'dana.whitlock@example.test',      '97330', '4417', 'total',    '2026-03-01', 19999, 1),
('cust_marcus',   'Marcus Iyer',        '5415550104', 'marcus.iyer@example.test',        '97402', '8802', 'standard', '',               0, 0),
('cust_priya',    'Priya Raman',        '5415550119', 'priya.raman@example.test',        '98104', '3361', 'plus',     '2026-01-20',  2999, 1),
('cust_glen',     'Glen Aldridge',      '5415550127', 'glen.aldridge@example.test',      '97213', '5540', 'total',    '2025-12-05', 19999, 1),
('cust_rosalind', 'Rosalind Baptiste',  '5415550133', 'rosalind.baptiste@example.test',  '97401', '7719', 'standard', '',               0, 0),
('cust_tomas',    'Tomas Ferreira',     '5415550146', 'tomas.ferreira@example.test',     '94110', '2208', 'plus',     '2026-05-10',  2999, 1),
('cust_amina',    'Amina Kalu',         '5415550152', 'amina.kalu@example.test',         '98661', '6673', 'plus',     '2026-04-02',  2999, 1),
('cust_victor',   'Victor Nunes',       '5415550165', 'victor.nunes@example.test',       '43081', '9014', 'total',    '2026-02-11', 19999, 1),
('cust_selina',   'Selina Cortez',      '5415550171', 'selina.cortez@example.test',      '97035', '1156', 'plus',     '2026-02-15',  2999, 1),
('cust_owen',     'Owen Tsai',          '5415550183', 'owen.tsai@example.test',          '43215', '4482', 'total',    '2025-09-20', 19999, 1),
('cust_nadia',    'Nadia Grant',        '5415550196', 'nadia.grant@example.test',        '98042', '7735', 'total',    '2026-06-01', 19999, 1),
('cust_felix',    'Felix Moreau',       '5415550108', 'felix.moreau@example.test',       '94612', '3390', 'plus',     '2026-03-22',  2999, 1),
('cust_grace',    'Grace Okonkwo',      '5415550112', 'grace.okonkwo@example.test',      '97005', '6628', 'total',    '2025-10-01', 19999, 1);

INSERT INTO orders (order_number, customer_id, order_date, status, fulfillment, seller_name, purchase_state, delivered_date, delivery_date, delivery_window, install, haul_away) VALUES
-- scheduled appliance delivery, 13 days out: a free delivery change
('KE-4471209', 'cust_dana',     '2026-07-20', 'scheduled', 'kestrel',     '',                    'OR', '',           '2026-08-14', '8am-12pm', 1, 1),
-- standard tier, delivered 22 days ago: past the 15-day window
('KE-4408117', 'cust_marcus',   '2026-07-08', 'delivered', 'kestrel',     '',                    'OR', '2026-07-10', '',           '',         0, 0),
-- the in-flight refund this customer is chasing
('KE-4399052', 'cust_marcus',   '2026-06-15', 'delivered', 'kestrel',     '',                    'OR', '2026-06-18', '',           '',         0, 0),
-- Plus member, activatable phone, day 12 of 14, opened, WA: the $45 fee applies
('KE-4462884', 'cust_priya',    '2026-07-18', 'delivered', 'kestrel',     '',                    'WA', '2026-07-20', '',           '',         0, 0),
-- Total member, activatable phone, day 17: 60 days does NOT apply, 14 does
('KE-4455031', 'cust_glen',     '2026-07-13', 'delivered', 'kestrel',     '',                    'OR', '2026-07-15', '',           '',         0, 0),
-- the scam persona's real order; her record carries no subscription at all
('KE-4431775', 'cust_rosalind', '2026-06-02', 'delivered', 'kestrel',     '',                    'OR', '2026-06-05', '',           '',         0, 0),
-- marketplace: Kestrel took the order, the seller owns the policy
('KE-4479002', 'cust_tomas',    '2026-07-24', 'delivered', 'marketplace', 'Northwind Supply Co', 'CA', '2026-07-27', '',           '',         0, 0),
-- recalled AND hazmat: no shipping label, no bench appointment
('KE-4483316', 'cust_amina',    '2026-07-26', 'delivered', 'kestrel',     '',                    'WA', '2026-07-29', '',           '',         0, 0),
-- recalled, not hazmat: isolates RECALLED_NO_SERVICE
('KE-4490224', 'cust_victor',   '2026-05-30', 'delivered', 'kestrel',     '',                    'OH', '2026-06-03', '',           '',         0, 0),
-- not yet shipped: cancellable outright
('KE-4498870', 'cust_selina',   '2026-07-30', 'processing','kestrel',     '',                    'OR', '',           '',           '',         0, 0),
-- drone, opened, purchased in Ohio: 15% would apply, the state exclusion kills it
('KE-4487740', 'cust_owen',     '2026-07-22', 'delivered', 'kestrel',     '',                    'OH', '2026-07-25', '',           '',         0, 0),
-- the same drone, opened, purchased in Washington: 15% applies
('KE-4492551', 'cust_nadia',    '2026-07-23', 'delivered', 'kestrel',     '',                    'WA', '2026-07-26', '',           '',         0, 0),
-- one new item (price-matchable) and one open-box item (excluded)
('KE-4495108', 'cust_felix',    '2026-07-27', 'delivered', 'kestrel',     '',                    'CA', '2026-07-29', '',           '',         0, 0),
-- bought under an active Total membership 9 months ago: covered, no deductible
('KE-4471860', 'cust_grace',    '2025-11-06', 'delivered', 'kestrel',     '',                    'OR', '2025-11-10', '',           '',         0, 0),
-- scheduled appliance delivery, 2 days out: a late delivery change ($29.99)
('KE-4500001', 'cust_nadia',    '2026-07-30', 'scheduled', 'kestrel',     '',                    'WA', '',           '2026-08-03', '8am-12pm', 1, 1);

INSERT INTO order_items (id, order_number, sku, name, category, price_cents, opened, activatable, restock_class, condition_grade, recalled, hazmat) VALUES
('it_01', 'KE-4471209', 'SKU-APP-2210', 'Northwind 26 cu ft French Door Refrigerator', 'appliance',   214999, 0, 0, 'none',        'new',                 0, 0),
('it_02', 'KE-4408117', 'SKU-CMP-1180', 'Kestrel Aurora 14 Laptop',                    'computing',    89999, 1, 0, 'none',        'new',                 0, 0),
('it_03', 'KE-4399052', 'SKU-CMP-2280', 'Kestrel Vista 27 Monitor',                    'computing',    32999, 1, 0, 'none',        'new',                 0, 0),
('it_04', 'KE-4462884', 'SKU-MOB-7702', 'Solstice X5 Smartphone',                      'mobile',      109999, 1, 1, 'activatable', 'new',                 0, 0),
('it_05', 'KE-4455031', 'SKU-MOB-7702', 'Solstice X5 Smartphone',                      'mobile',      109999, 1, 1, 'activatable', 'new',                 0, 0),
('it_06', 'KE-4431775', 'SKU-BWM-0450', 'Bellwether Ease 4 Phone',                     'mobile',       14999, 1, 1, 'activatable', 'new',                 0, 0),
('it_07', 'KE-4479002', 'SKU-AUD-3390', 'Corva Studio Headphones',                     'audio',        24999, 1, 0, 'none',        'new',                 0, 0),
('it_08', 'KE-4483316', 'SKU-PWR-5510', 'Voltbank 20K Power Bank',                     'accessories',   7999, 1, 0, 'none',        'new',                 1, 1),
('it_09', 'KE-4490224', 'SKU-HOM-6120', 'Emberline Ceramic Space Heater',              'home',         14999, 1, 0, 'none',        'new',                 1, 0),
('it_10', 'KE-4498870', 'SKU-AUD-3350', 'Corva Mini Bluetooth Speaker',                'audio',        18999, 0, 0, 'none',        'new',                 0, 0),
('it_11', 'KE-4487740', 'SKU-DRN-4400', 'Skyward Vireo 3 Drone',                       'drone',        99999, 1, 0, 'percent_15',  'new',                 0, 0),
('it_12', 'KE-4492551', 'SKU-DRN-4400', 'Skyward Vireo 3 Drone',                       'drone',        99999, 1, 0, 'percent_15',  'new',                 0, 0),
('it_13', 'KE-4495108', 'SKU-AUD-7720', 'Aurelian Halo Soundbar',                      'audio',        54999, 0, 0, 'none',        'new',                 0, 0),
('it_14', 'KE-4495108', 'SKU-TV-4410',  'Kestrel Vista 55 4K TV',                      'television',   39999, 1, 0, 'none',        'open_box_excellent',  0, 0),
('it_15', 'KE-4471860', 'SKU-CMP-9930', 'Kestrel Aurora Pro 16 Laptop',                'computing',   149999, 1, 0, 'none',        'new',                 0, 0),
('it_16', 'KE-4500001', 'SKU-APP-7740', 'Emberline Induction Range',                   'appliance',   149999, 0, 0, 'none',        'new',                 0, 0),
('it_17', 'KE-4495108', 'SKU-AUD-8820', 'Aurelian Soundbar Mini',                      'audio',        44999, 0, 0, 'none',        'new',                 0, 0);

INSERT INTO protection_plans (id, customer_id, order_number, sku, plan_name, start_date, end_date, deductible_cents) VALUES
('pp_priya', 'cust_priya', 'KE-4462884', 'SKU-MOB-7702', 'TechCrew Protect Mobile', '2026-07-20', '2028-07-20', 14900),
('pp_nadia', 'cust_nadia', 'KE-4492551', 'SKU-DRN-4400', 'TechCrew Protect Drone',  '2026-07-26', '2028-07-26', 24900);

INSERT INTO service_slots (date, service_type, time_window, available) VALUES
('2026-08-03', 'bench',   '11:00am',   1),
('2026-08-04', 'bench',   '2:00pm',    1),
('2026-08-05', 'bench',   '10:00am',   1),
('2026-08-06', 'bench',   '3:00pm',    1),
('2026-08-07', 'bench',   '11:00am',   1),
('2026-08-08', 'bench',   '1:00pm',    1),
('2026-08-05', 'in_home', '8am-12pm',  1),
('2026-08-07', 'in_home', '12pm-4pm',  1),
('2026-08-10', 'in_home', '8am-12pm',  1),
('2026-08-12', 'in_home', '12pm-4pm',  1),
('2026-08-02', 'remote',  '9:00am',    1),
('2026-08-03', 'remote',  '1:00pm',    1),
('2026-08-04', 'remote',  '9:00am',    1);

INSERT INTO refunds (rma_number, customer_id, order_number, received_date, amount_cents, stage, posts_by, method) VALUES
('RMA-778201', 'cust_marcus', 'KE-4399052', '2026-07-28', 32999, 'processing', '2026-08-05', 'Visa ending 8802');

INSERT INTO outbound_contacts (id, phone, email, channel, contact_date, summary) VALUES
('oc_01', '5415550188', 'dana.whitlock@example.test', 'sms', '2026-07-31', 'Delivery reminder for order KE-4471209, arriving August 14 between 8am and 12pm.'),
('oc_02', '5415550112', 'grace.okonkwo@example.test', 'email', '2026-07-24', 'TechCrew Bench repair on order KE-4471860 is ready for pickup.');
