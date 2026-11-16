SELECT o.id, o.amount, c.region
FROM orders AS o
JOIN customers AS c ON o.customer_id = c.id
WHERE o.status = 'paid' AND o.amount >= 100;
