// Phase 4 W3 C4 iter 10 — `ApprovalClient` unit tests.
//
// Two layers of coverage:
//
//   1. **Body shape** — assert the encoded `ApprovalDecision` JSON
//      matches `chat_approve.rs:34-37` so a server-side schema drift
//      surfaces as a failed test instead of a silent runtime 400.
//   2. **Response decoding** — given a stub `ApprovalResponse` JSON,
//      assert the client maps it to the typed shape.
//
// We *don't* exercise the URLSession-backed `URLSessionApprovalClient`
// here — its only job is to hold a session and forward to `data(for:)`,
// which `URLProtocolStub`-style tests already cover well in the
// broader project. The wire shape is the load-bearing contract.

import XCTest

@testable import CorlinmanCore

final class ApprovalClientTests: XCTestCase {

    // MARK: - Body shape

    /// Approve case: no deny_message, scope=`once` (the default).
    func test_approvalDecision_approveBody_matchesWireSchema() throws {
        let decision = ApprovalDecision(
            callId: "call_abc",
            approved: true,
            scope: .once
        )
        let encoded = try JSONEncoder().encode(decision)
        let parsed = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]

        XCTAssertEqual(parsed?["call_id"] as? String, "call_abc")
        XCTAssertEqual(parsed?["approved"] as? Bool, true)
        XCTAssertEqual(parsed?["scope"] as? String, "once")
        // `deny_message` is `nil` → either omitted or `<null>`. Either
        // is acceptable on the wire (`chat_approve.rs:36` makes it
        // optional). We keep the assertion permissive to match.
        if let raw = parsed?["deny_message"] {
            XCTAssertTrue(raw is NSNull,
                "non-null deny_message must be null when approved=true; got \(raw)")
        }
    }

    /// Deny case with explicit message + `session` scope.
    func test_approvalDecision_denyBody_matchesWireSchema() throws {
        let decision = ApprovalDecision(
            callId: "call_xyz",
            approved: false,
            scope: .session,
            denyMessage: "policy violation"
        )
        let encoded = try JSONEncoder().encode(decision)
        let parsed = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]

        XCTAssertEqual(parsed?["approved"] as? Bool, false)
        XCTAssertEqual(parsed?["scope"] as? String, "session")
        XCTAssertEqual(parsed?["deny_message"] as? String, "policy violation")
    }

    // MARK: - Response decoding

    /// Server's success echo decodes cleanly into `ApprovalResponse`.
    func test_approvalResponse_decodesServerEcho() throws {
        let json = #"""
        {"turn_id":"t1","call_id":"call_abc","decision":"approved"}
        """#
        let decoded = try JSONDecoder().decode(
            ApprovalResponse.self,
            from: Data(json.utf8)
        )
        XCTAssertEqual(decoded.turn_id, "t1")
        XCTAssertEqual(decoded.call_id, "call_abc")
        XCTAssertEqual(decoded.decision, "approved")
    }

    /// `URLSessionApprovalClient` rejects empty bearer up-front so a
    /// pre-onboarding state surfaces as a typed error instead of a
    /// `401` from the gateway.
    func test_urlSessionApprovalClient_missingBearerThrows() async {
        let client = URLSessionApprovalClient(
            baseURL: URL(string: "https://gateway.example.com")!,
            bearerProvider: { nil }
        )
        do {
            _ = try await client.submit(
                turnId: "t",
                decision: ApprovalDecision(callId: "c", approved: true, scope: .once)
            )
            XCTFail("missing bearer should throw")
        } catch ApprovalClientError.missingBearer {
            // expected
        } catch {
            XCTFail("expected .missingBearer, got \(error)")
        }
    }

    /// Empty `turnId` would otherwise generate the URL
    /// `…/v1/chat/completions//approve` and confuse routers.
    func test_urlSessionApprovalClient_emptyTurnIdThrows() async {
        let client = URLSessionApprovalClient(
            baseURL: URL(string: "https://gateway.example.com")!,
            bearerProvider: { "ck_test" }
        )
        do {
            _ = try await client.submit(
                turnId: "",
                decision: ApprovalDecision(callId: "c", approved: true, scope: .once)
            )
            XCTFail("empty turnId should throw")
        } catch ApprovalClientError.invalidURL {
            // expected
        } catch {
            XCTFail("expected .invalidURL, got \(error)")
        }
    }
}
